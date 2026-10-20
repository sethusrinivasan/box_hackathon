"""
agent/cdc/duckdb_applier.py — DuckDB mutation engine, schema applier, and S3 manager.

Applies decoded WAL changes (INSERT, UPDATE, DELETE) into an in-process
DuckDB database file. Enforces metadata watermarks (__cdc_metadata),
manages WAL checkpoints, and handles atomic S3 synchronization.
"""
from __future__ import annotations

import datetime
import logging
import os
import shutil
from typing import Any

import boto3
from botocore.exceptions import ClientError
import duckdb

from agent.cdc.wal_parser import WalChange

logger = logging.getLogger("reality_check.cdc.duckdb")

METADATA_TABLE = "__cdc_metadata"
MAX_DUCKDB_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB bound


class DuckDbApplier:
    """Manages local DuckDB mutations, metadata watermarks, and checkpoints."""

    def __init__(self, db_path: str = "/tmp/analytics.duckdb") -> None:  # nosec B108
        self.db_path = db_path
        self.conn: duckdb.DuckDBPyConnection | None = None
        self._ensure_connection()

    def _ensure_connection(self) -> None:
        if self.conn is None:
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
            self.conn = duckdb.connect(self.db_path)
            self._init_metadata_table()

    def _init_metadata_table(self) -> None:
        assert self.conn is not None
        self.conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {METADATA_TABLE} (
                key VARCHAR PRIMARY KEY,
                value VARCHAR,
                updated_at TIMESTAMP
            );
        """)

    def get_watermark(self) -> dict[str, str]:
        """Fetch current LSN and timestamp watermarks from __cdc_metadata."""
        assert self.conn is not None
        rows = self.conn.execute(f"SELECT key, value FROM {METADATA_TABLE}").fetchall()  # nosec B608
        return {r[0]: r[1] for r in rows}

    def set_watermark(
        self,
        last_lsn: int,
        last_commit_time: datetime.datetime | str | None = None,
        sync_mode: str = "INCREMENTAL",
    ) -> None:
        """Update LSN and timestamp watermarks atomically."""
        assert self.conn is not None
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
        ts_val = ""
        if isinstance(last_commit_time, datetime.datetime):
            ts_val = last_commit_time.isoformat()
        elif last_commit_time:
            ts_val = str(last_commit_time)

        updates = [
            ("last_applied_lsn", str(last_lsn)),
            ("last_commit_timestamp", ts_val),
            ("sync_mode", sync_mode),
            ("last_sync_timestamp", now_str),
        ]
        for k, v in updates:
            self.conn.execute(
                f"""
                INSERT OR REPLACE INTO {METADATA_TABLE} (key, value, updated_at)
                VALUES (?, ?, ?)
                """,
                [k, v, now_str],
            )

    def full_reinit_table(
        self,
        table_name: str,
        columns: list[str],
        column_types: list[str],
        rows: list[tuple[Any, ...]],
        primary_keys: list[str] | None = None,
        snapshot_lsn: int = 0,
    ) -> None:
        """Reinitialize a table completely from an Aurora snapshot."""
        assert self.conn is not None
        logger.info("Performing full table re-init for '%s' (%d rows)", table_name, len(rows))

        # Drop existing table
        self.conn.execute(f"DROP TABLE IF EXISTS {table_name}")

        # Build schema
        col_defs = []
        for col, ctype in zip(columns, column_types):
            col_defs.append(f'"{col}" {ctype}')

        pk_clause = ""
        if primary_keys:
            pk_cols = ", ".join(f'"{k}"' for k in primary_keys)
            pk_clause = f", PRIMARY KEY ({pk_cols})"

        create_sql = f'CREATE TABLE "{table_name}" ({", ".join(col_defs)}{pk_clause});'
        self.conn.execute(create_sql)

        # Bulk insert snapshot rows
        if rows:
            placeholders = ", ".join(["?"] * len(columns))
            insert_sql = f'INSERT INTO "{table_name}" VALUES ({placeholders})'  # nosec B608
            self.conn.executemany(insert_sql, rows)

        self.set_watermark(
            last_lsn=snapshot_lsn,
            last_commit_time=datetime.datetime.now(datetime.timezone.utc),
            sync_mode="FULL_REINIT",
        )
        self.checkpoint()

    def apply_changes(self, changes: list[WalChange]) -> int:
        """Apply a sequence of decoded WAL mutations within DuckDB."""
        assert self.conn is not None
        applied_count = 0
        last_lsn = 0
        last_commit_time = None

        for change in changes:
            table = change.table
            if change.op == "INSERT":
                self._apply_insert(table, change.values, change.keys)
                applied_count += 1
            elif change.op == "UPDATE":
                self._apply_update(table, change.values, change.keys)
                applied_count += 1
            elif change.op == "DELETE":
                self._apply_delete(table, change.keys)
                applied_count += 1

            if change.lsn:
                last_lsn = max(last_lsn, change.lsn)
            if change.commit_time:
                last_commit_time = change.commit_time

        if last_lsn > 0:
            self.set_watermark(last_lsn=last_lsn, last_commit_time=last_commit_time)

        return applied_count

    def _apply_insert(self, table: str, values: dict[str, Any], keys: dict[str, Any]) -> None:
        assert self.conn is not None
        if not values:
            return

        # If keys exist, delete existing row first to guarantee idempotent UPSERT behavior
        if keys:
            self._apply_delete(table, keys)

        cols = list(values.keys())
        quoted_cols = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join(["?"] * len(cols))
        sql = f'INSERT INTO "{table}" ({quoted_cols}) VALUES ({placeholders})'  # nosec B608
        self.conn.execute(sql, [values[c] for c in cols])

    def _apply_update(self, table: str, values: dict[str, Any], keys: dict[str, Any]) -> None:
        assert self.conn is not None
        if not keys:
            return

        # If no values changed, nothing to do
        set_cols = [c for c in values.keys() if c not in keys]
        if not set_cols:
            return

        set_clauses = ", ".join([f'"{c}" = ?' for c in set_cols])
        where_clauses = " AND ".join([f'"{k}" = ?' for k in keys.keys()])
        sql = f'UPDATE "{table}" SET {set_clauses} WHERE {where_clauses}'  # nosec B608

        params = [values[c] for c in set_cols] + [keys[k] for k in keys.keys()]
        self.conn.execute(sql, params)

    def _apply_delete(self, table: str, keys: dict[str, Any]) -> None:
        assert self.conn is not None
        if not keys:
            return
        where_clauses = " AND ".join([f'"{k}" = ?' for k in keys.keys()])
        sql = f'DELETE FROM "{table}" WHERE {where_clauses}'  # nosec B608
        self.conn.execute(sql, list(keys.values()))

    def checkpoint(self) -> None:
        """Execute DuckDB CHECKPOINT to fold pending changes from WAL into main file."""
        assert self.conn is not None
        logger.debug("Executing DuckDB CHECKPOINT on %s", self.db_path)
        self.conn.execute("CHECKPOINT;")

    def get_file_size_bytes(self) -> int:
        if os.path.exists(self.db_path):
            return os.path.getsize(self.db_path)
        return 0

    def close(self) -> None:
        if self.conn:
            try:
                self.conn.close()
            except Exception as e:
                logger.warning("Error closing DuckDB connection: %s", e)
            finally:
                self.conn = None


class DuckDbS3Storage:
    """Handles S3 pull and atomic push for DuckDB database files."""

    def __init__(self, s3_client: Any | None = None) -> None:
        self.s3 = s3_client or boto3.client("s3")

    def download_if_exists(self, bucket: str, key: str, local_path: str) -> bool:
        """Download database file from S3 if present.

        Returns True if downloaded, False if not found.
        """
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        try:
            self.s3.download_file(bucket, key, local_path)
            logger.info("Downloaded %s/%s to %s (size: %d bytes)", bucket, key, local_path, os.path.getsize(local_path))
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in ("404", "NoSuchKey", "NotFound"):
                logger.info("No existing DuckDB file found at s3://%s/%s (fresh start)", bucket, key)
                return False
            logger.error("Failed to download s3://%s/%s: %s", bucket, key, e)
            raise

    def upload_file(
        self,
        bucket: str,
        key: str,
        local_path: str,
        storage_class: str = "STANDARD",
    ) -> str:
        """Upload DuckDB file to S3 atomically.

        Explicitly sets StorageClass (default: STANDARD - cheapest tier for < 50 MB
        with frequent 5-minute overwrites; avoids Glacier/IA minimum storage durations
        and per-GB retrieval fees).

        Returns the uploaded object ETag.
        """
        file_size = os.path.getsize(local_path)
        if file_size > MAX_DUCKDB_FILE_SIZE_BYTES:
            logger.warning(
                "DuckDB file size (%d bytes) exceeds 50 MB threshold!",
                file_size
            )

        with open(local_path, "rb") as f:
            resp = self.s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=f,
                ContentType="application/octet-stream",
                StorageClass=storage_class,
                Metadata={
                    "cdc-sync-timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "cdc-file-size": str(file_size),
                },
            )
        etag = resp.get("ETag", "").strip('"')
        logger.info(
            "Uploaded %s to s3://%s/%s (tier: %s, size: %d bytes, etag: %s)",
            local_path,
            bucket,
            key,
            storage_class,
            file_size,
            etag,
        )
        return etag
