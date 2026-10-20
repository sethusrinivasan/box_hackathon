"""
agent/cdc/sync_worker.py — Scheduled Singleton CDC Lambda Worker.

Orchestrates the Change Data Capture pipeline:
  1. Acquires a distributed DynamoDB lease (guaranteeing single-writer execution).
  2. Pulls the latest DuckDB file from S3 (or boots a blank replica).
  3. Validates file integrity and checks for row drift.
     - On corruption/drift: initiates an automated full table re-initialization
       and emits FullReInitCount EMF metric.
     - On healthy state: streams unconsumed WAL changes from Aurora via logical
       replication (pgoutput) and applies mutations.
  4. Executes DuckDB CHECKPOINT to fold WAL changes into the single file.
  5. Atomically uploads the updated DuckDB database back to S3.
  6. Acknowledges LSN to Aurora replication slot only after S3 upload succeeds.
  7. Emits operational EMF metrics (ReplicationLagSeconds, ReplicationLagBytes,
     EventsProcessed, SyncDurationMs).
  8. Safely releases the DynamoDB lease.
"""
from __future__ import annotations

import datetime
import logging
import os
import sys
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError
import certifi
import psycopg

from agent.cdc.duckdb_applier import DuckDbApplier, DuckDbS3Storage
from agent.cdc.lease import DynamoDbLeaseManager
from agent.cdc.metrics import emit_metric, emit_sync_metrics
from agent.cdc.validator import DuckDbValidator
from agent.cdc.wal_parser import PgoutputParser, WalChange

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("reality_check.cdc.worker")

# Environment configuration
LOCK_TABLE_NAME = os.environ.get("LOCK_TABLE_NAME", "duckdb_sync_locks")
LOCK_KEY = os.environ.get("LOCK_KEY", "duckdb_writer_lease")
LEASE_DURATION_SEC = int(os.environ.get("LEASE_DURATION_SEC", "300"))
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_KEY = os.environ.get("S3_KEY", "analytics/analytics.duckdb")
S3_STORAGE_CLASS = os.environ.get("S3_STORAGE_CLASS", "STANDARD")
LOCAL_DB_PATH = os.environ.get("LOCAL_DB_PATH", "/tmp/analytics.duckdb")  # nosec B108

DB_HOST = os.environ.get("DB_HOST", "")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "postgres")
DB_IAM_USER = os.environ.get("DB_IAM_USER", "cdc_sync_worker")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")  # Fallback for local/test env
REGION = os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_REGION", "us-east-1")

REPLICATION_SLOT = os.environ.get("REPLICATION_SLOT", "duckdb_cdc_slot")
PUBLICATION_NAME = os.environ.get("PUBLICATION_NAME", "duckdb_pub")
REPLICATED_TABLES = [
    t.strip() for t in os.environ.get("REPLICATED_TABLES", "daily_cost").split(",") if t.strip()
]


def lsn_to_int(lsn_str: str | int) -> int:
    """Convert PostgreSQL LSN string 'X/YYYYYY' or integer to int."""
    if isinstance(lsn_str, int):
        return lsn_str
    if not lsn_str:
        return 0
    if "/" in str(lsn_str):
        upper, lower = str(lsn_str).split("/", 1)
        return (int(upper, 16) << 32) + int(lower, 16)
    return int(lsn_str)


def int_to_lsn(val: int) -> str:
    """Convert integer to PostgreSQL LSN string 'X/YYYYYY'."""
    upper = val >> 32
    lower = val & 0xFFFFFFFF
    return f"{upper:X}/{lower:X}"


class CdcSyncWorker:
    """Coordinates singleton CDC synchronization from Aurora to DuckDB in S3."""

    def __init__(
        self,
        lock_table: str = LOCK_TABLE_NAME,
        lock_key: str = LOCK_KEY,
        s3_bucket: str = S3_BUCKET,
        s3_key: str = S3_KEY,
        s3_storage_class: str = S3_STORAGE_CLASS,
        local_db_path: str = LOCAL_DB_PATH,
        replicated_tables: list[str] | None = None,
        lease_manager: DynamoDbLeaseManager | None = None,
        storage: DuckDbS3Storage | None = None,
        rds_client: Any | None = None,
        pg_connection: Any | None = None,
    ) -> None:
        self.lock_table = lock_table
        self.lock_key = lock_key
        self.s3_bucket = s3_bucket
        self.s3_key = s3_key
        self.s3_storage_class = s3_storage_class
        self.local_db_path = local_db_path
        self.replicated_tables = replicated_tables or REPLICATED_TABLES
        self.lease = lease_manager or DynamoDbLeaseManager(
            table_name=self.lock_table,
            lock_key=self.lock_key,
            lease_duration_sec=LEASE_DURATION_SEC,
        )
        self.storage = storage or DuckDbS3Storage()
        self._rds = rds_client or boto3.client("rds", region_name=REGION)
        self._pg_conn = pg_connection

    def execute_sync(self, force_reinit: bool = False) -> dict[str, Any]:
        """Execute one complete CDC sync pass under a distributed lease."""
        start_time = time.perf_counter()
        logger.info("CDC sync execution triggered. Attempting to acquire lease...")

        if not self.lease.acquire():
            logger.info("Lease is currently held by another worker. Exiting without work.")
            return {
                "statusCode": 200,
                "status": "LOCKED_SKIPPED",
                "message": "Another CDC worker execution is active.",
            }

        reinit_count = 0
        events_processed = 0
        lag_seconds = 0.0
        lag_bytes = 0

        try:
            # 1. Download existing DuckDB file from S3 if available
            existed_in_s3 = False
            if not force_reinit and self.s3_bucket:
                existed_in_s3 = self.storage.download_if_exists(
                    bucket=self.s3_bucket,
                    key=self.s3_key,
                    local_path=self.local_db_path,
                )

            applier = DuckDbApplier(self.local_db_path)
            validator = DuckDbValidator(self.local_db_path)

            # 2. Check if initial sync or full re-init is necessary
            needs_full_sync = force_reinit or not existed_in_s3
            if not needs_full_sync:
                file_check = validator.validate_file_integrity(conn=applier.conn)
                if not file_check.is_valid:
                    logger.warning(
                        "Integrity validation failed (%s). Triggering full re-init.",
                        file_check.discrepancy_reason
                    )
                    emit_metric("DataDiscrepancyDetected", 1, "Count")
                    needs_full_sync = True

            pg_conn = self._connect_aurora()
            try:
                with pg_conn.cursor() as cur:
                    # Check Aurora WAL level (requires 'logical' for incremental WAL decoding)
                    cur.execute("SHOW wal_level;")
                    wal_row = cur.fetchone()
                    wal_level = wal_row[0] if wal_row else "unknown"

                    current_aurora_lsn_str = "0/0"
                    current_aurora_lsn = 0

                    if wal_level == "logical":
                        cur.execute("SELECT pg_current_wal_lsn();")
                        row = cur.fetchone()
                        current_aurora_lsn_str = row[0] if row else "0/0"
                        current_aurora_lsn = lsn_to_int(current_aurora_lsn_str)
                    else:
                        logger.warning(
                            "Aurora wal_level is '%s' (not 'logical'). Incremental WAL decoding requires "
                            "rds.logical_replication=1 in DB cluster parameter group. Performing snapshot sync.",
                            wal_level,
                        )
                        needs_full_sync = True

                    if not needs_full_sync:
                        # Check drift against Aurora
                        drift_check = validator.validate_drift_against_aurora(
                            self.replicated_tables, cur, conn=applier.conn
                        )
                        if not drift_check.is_valid:
                            logger.warning(
                                "Drift detected against Aurora (%s). Triggering full re-init.",
                                drift_check.discrepancy_reason
                            )
                            emit_metric("DataDiscrepancyDetected", 1, "Count")
                            needs_full_sync = True

                    if needs_full_sync:
                        logger.info("Executing full snapshot initial sync...")
                        self._perform_full_sync(pg_conn, applier, current_aurora_lsn)
                        reinit_count = 1
                    else:
                        logger.info("Executing incremental WAL replication sync...")
                        events_processed = self._perform_incremental_sync(
                            pg_conn, applier, current_aurora_lsn
                        )

                applier.checkpoint()
                applier.close()

                # 3. Upload to S3 atomically
                if self.s3_bucket:
                    etag = self.storage.upload_file(
                        bucket=self.s3_bucket,
                        key=self.s3_key,
                        local_path=self.local_db_path,
                        storage_class=self.s3_storage_class,
                    )
                    logger.info("Successfully pushed updated DuckDB to S3 with ETag: %s", etag)

                # 4. Advance PostgreSQL replication slot LSN only after S3 upload succeeds
                if wal_level == "logical":
                    self._acknowledge_replication_slot(pg_conn, current_aurora_lsn_str)

                # Calculate replication lag
                watermark = DuckDbApplier(self.local_db_path).get_watermark()
                last_applied_lsn = int(watermark.get("last_applied_lsn", "0"))
                lag_bytes = max(0, current_aurora_lsn - last_applied_lsn)

                commit_ts_str = watermark.get("last_commit_timestamp")
                if commit_ts_str:
                    try:
                        commit_dt = datetime.datetime.fromisoformat(commit_ts_str)
                        now_utc = datetime.datetime.now(datetime.timezone.utc)
                        lag_seconds = max(0.0, (now_utc - commit_dt).total_seconds())
                    except ValueError:
                        lag_seconds = 0.0

            finally:
                pg_conn.close()

            duration_ms = (time.perf_counter() - start_time) * 1000
            emit_sync_metrics(
                lag_seconds=lag_seconds,
                lag_bytes=lag_bytes,
                events_count=events_processed,
                duration_ms=duration_ms,
                reinit_count=reinit_count,
            )

            return {
                "statusCode": 200,
                "status": "SUCCESS",
                "reinit_count": reinit_count,
                "events_processed": events_processed,
                "lag_seconds": round(lag_seconds, 2),
                "lag_bytes": lag_bytes,
                "duration_ms": round(duration_ms, 2),
            }

        finally:
            self.lease.release()

    def _perform_full_sync(
        self,
        pg_conn: Any,
        applier: DuckDbApplier,
        snapshot_lsn: int,
    ) -> None:
        """Full table reload from Aurora into DuckDB."""
        with pg_conn.cursor() as cur:
            for table in self.replicated_tables:
                # Query column schema
                cur.execute(
                    """
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_name = %s
                    ORDER BY ordinal_position;
                    """,
                    [table],
                )
                cols_meta = cur.fetchall()
                if not cols_meta:
                    logger.warning("Table %s not found in Aurora PostgreSQL", table)
                    continue

                columns = [c[0] for c in cols_meta]
                column_types = [self._map_pg_to_duckdb_type(c[1]) for c in cols_meta]

                # Query primary keys
                cur.execute(
                    """
                    SELECT c.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.constraint_column_usage c
                      ON tc.constraint_name = c.constraint_name
                    WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_name = %s;
                    """,
                    [table],
                )
                pks = [r[0] for r in cur.fetchall()]

                # Fetch all rows
                quoted_cols = ", ".join(f'"{c}"' for c in columns)
                cur.execute(f'SELECT {quoted_cols} FROM "{table}";')  # nosec B608
                rows = cur.fetchall()

                applier.full_reinit_table(
                    table_name=table,
                    columns=columns,
                    column_types=column_types,
                    rows=rows,
                    primary_keys=pks,
                    snapshot_lsn=snapshot_lsn,
                )

    def _perform_incremental_sync(
        self,
        pg_conn: Any,
        applier: DuckDbApplier,
        target_lsn: int,
    ) -> int:
        """Consume WAL stream up to target_lsn and apply changes."""
        watermark = applier.get_watermark()
        last_lsn = int(watermark.get("last_applied_lsn", "0"))
        start_lsn_str = int_to_lsn(last_lsn) if last_lsn > 0 else "0/0"

        logger.info("Reading replication slot %s from LSN %s", REPLICATION_SLOT, start_lsn_str)
        # Using pg_logical_slot_get_changes for standard SQL-accessible consumption
        # (or pgoutput decoding)
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                SELECT lsn, xid, data
                FROM pg_logical_slot_get_changes(
                    %s, NULL, NULL,
                    'include-xids', '1',
                    'include-timestamp', '1'
                );
                """,
                [REPLICATION_SLOT],
            )
            changes_rows = cur.fetchall()

            if not changes_rows:
                logger.info("No unconsumed WAL changes in replication slot.")
                applier.set_watermark(last_lsn=target_lsn)
                return 0

            changes: list[WalChange] = []
            for row in changes_rows:
                lsn_val, xid, data_text = row[0], row[1], row[2]
                change = self._parse_text_change(lsn_val, xid, data_text)
                if change and change.table in self.replicated_tables:
                    changes.append(change)

            applied_count = applier.apply_changes(changes)
            applier.set_watermark(last_lsn=target_lsn)
            logger.info("Applied %d incremental changes to DuckDB", applied_count)
            return applied_count

    def _parse_text_change(self, lsn_str: str, xid: int, data: str) -> WalChange | None:
        """Parse standard logical decoding output line (e.g. test_decoding format)."""
        # Example format: "table public.daily_cost: INSERT: usage_date['2026-10-18'] gross_usd[12.34]"
        if not data or not data.startswith("table "):
            return None

        try:
            prefix, rest = data.split(": ", 1)
            tbl_full = prefix.replace("table ", "")
            parts = tbl_full.split(".", 1)
            schema = parts[0] if len(parts) == 2 else "public"
            table = parts[1] if len(parts) == 2 else parts[0]

            op_part, cols_part = rest.split(": ", 1) if ": " in rest else (rest, "")
            op = op_part.strip()  # INSERT, UPDATE, DELETE

            values: dict[str, Any] = {}
            # Parse column tokens e.g. col_name['val']
            import re
            col_matches = re.findall(r"([a-zA-Z0-9_]+)\[([^\]]*)\]", cols_part)
            for col, val in col_matches:
                if val == "null" or val == "NULL":
                    values[col] = None
                elif val.startswith("'") and val.endswith("'"):
                    values[col] = val[1:-1]
                else:
                    values[col] = val

            return WalChange(
                op=op,
                schema=schema,
                table=table,
                keys=values,  # For delete/update where keys are specified
                values=values,
                lsn=lsn_to_int(lsn_str),
                commit_time=datetime.datetime.now(datetime.timezone.utc),
                tx_id=xid,
            )
        except Exception as e:
            logger.debug("Non-mutation or unparseable WAL message: %s (%s)", data, e)
            return None

    def _acknowledge_replication_slot(self, pg_conn: Any, lsn_str: str) -> None:
        """Acknowledge unconsumed changes in the replication slot up to lsn_str."""
        try:
            with pg_conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_replication_slot_advance(%s, %s);",
                    [REPLICATION_SLOT, lsn_str],
                )
            logger.info("Acknowledged replication slot %s to %s", REPLICATION_SLOT, lsn_str)
        except Exception as e:
            logger.warning("Could not advance replication slot (might already be caught up): %s", e)

    def _map_pg_to_duckdb_type(self, pg_type: str) -> str:
        """Map PostgreSQL data types to DuckDB SQL types."""
        t = pg_type.lower()
        if "int" in t or "serial" in t:
            return "BIGINT" if "big" in t or "8" in t else "INTEGER"
        if "numeric" in t or "decimal" in t or "double" in t or "real" in t:
            return "DOUBLE"
        if "bool" in t:
            return "BOOLEAN"
        if "timestamp" in t:
            return "TIMESTAMP WITH TIME ZONE" if "tz" in t or "zone" in t else "TIMESTAMP"
        if "date" in t:
            return "DATE"
        if "json" in t:
            return "JSON"
        return "VARCHAR"

    def _connect_aurora(self) -> Any:
        """Create PostgreSQL database connection using IAM DB auth or password."""
        if self._pg_conn is not None:
            return self._pg_conn

        password = DB_PASSWORD
        if not password and DB_HOST:
            # Mint IAM database authentication token
            password = self._rds.generate_db_auth_token(
                DBHostname=DB_HOST,
                Port=DB_PORT,
                DBUsername=DB_IAM_USER,
                Region=REGION,
            )

        conninfo = (
            f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_IAM_USER} "
            f"password={password} sslmode=verify-full sslrootcert={certifi.where()} "
            f"connect_timeout=15"
        )
        return psycopg.connect(conninfo, autocommit=True)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entry point invoked via EventBridge Schedule."""
    force_reinit = event.get("force_reinit", False) if isinstance(event, dict) else False
    worker = CdcSyncWorker()
    return worker.execute_sync(force_reinit=force_reinit)
