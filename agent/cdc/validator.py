"""
agent/cdc/validator.py — Self-checking integrity and drift detection validator.

Performs health checks on the DuckDB database file, validating header
integrity, watermark timestamps, queryability, and schema drift against
source Aurora PostgreSQL. Detects discrepancies that necessitate automated full re-init.
"""
from __future__ import annotations

import datetime
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import duckdb

logger = logging.getLogger("reality_check.cdc.validator")


@dataclass
class ValidationResult:
    is_valid: bool
    discrepancy_reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class DuckDbValidator:
    """Validates local DuckDB file integrity and detects data drift against Aurora."""

    def __init__(self, db_path: str = "/tmp/analytics.duckdb") -> None:  # nosec B108
        self.db_path = db_path

    def validate_file_integrity(self, conn: Any | None = None) -> ValidationResult:
        """Check if file exists, opens cleanly, and has valid metadata."""
        if not os.path.exists(self.db_path):
            return ValidationResult(
                is_valid=False,
                discrepancy_reason="DuckDB file does not exist on disk",
            )

        if os.path.getsize(self.db_path) == 0:
            return ValidationResult(
                is_valid=False,
                discrepancy_reason="DuckDB file is 0 bytes (corrupt or empty)",
            )

        owns_conn = False
        try:
            if conn is None:
                conn = duckdb.connect(self.db_path, read_only=True)
                owns_conn = True
            try:
                # Check metadata table
                tables = [r[0] for r in conn.execute("SHOW TABLES;").fetchall()]
                if "__cdc_metadata" not in tables:
                    return ValidationResult(
                        is_valid=False,
                        discrepancy_reason="Missing __cdc_metadata table",
                        details={"tables": tables},
                    )

                metadata_rows = conn.execute(
                    "SELECT key, value FROM __cdc_metadata"
                ).fetchall()
                meta = {r[0]: r[1] for r in metadata_rows}

                # Validate LSN watermark
                lsn_val = meta.get("last_applied_lsn")
                if not lsn_val or not lsn_val.isdigit():
                    return ValidationResult(
                        is_valid=False,
                        discrepancy_reason=f"Invalid or missing last_applied_lsn: {lsn_val}",
                        details={"metadata": meta},
                    )

                # Validate timestamp watermark
                ts_val = meta.get("last_commit_timestamp")
                if ts_val:
                    try:
                        # Allow parsing ISO format
                        parsed_ts = datetime.datetime.fromisoformat(ts_val)
                        now_utc = datetime.datetime.now(datetime.timezone.utc)
                        # Sanity check: timestamp cannot be more than 1 day in the future
                        if parsed_ts > now_utc + datetime.timedelta(days=1):
                            return ValidationResult(
                                is_valid=False,
                                discrepancy_reason=f"Future timestamp detected: {ts_val}",
                                details={"metadata": meta},
                            )
                    except ValueError:
                        return ValidationResult(
                            is_valid=False,
                            discrepancy_reason=f"Unparseable commit timestamp: {ts_val}",
                            details={"metadata": meta},
                        )

                return ValidationResult(
                    is_valid=True,
                    details={"tables": tables, "metadata": meta},
                )
            finally:
                if owns_conn:
                    conn.close()
        except Exception as e:
            logger.warning("DuckDB corruption detected during validation: %s", e)
            return ValidationResult(
                is_valid=False,
                discrepancy_reason=f"DuckDB engine error / file corruption: {str(e)}",
            )

    def validate_drift_against_aurora(
        self,
        tables_to_check: list[str],
        pg_cursor: Any,
        conn: Any | None = None,
    ) -> ValidationResult:
        """Compares row counts between DuckDB and source Aurora PostgreSQL."""
        file_check = self.validate_file_integrity(conn=conn)
        if not file_check.is_valid:
            return file_check

        owns_conn = False
        if conn is None:
            conn = duckdb.connect(self.db_path, read_only=True)
            owns_conn = True

        try:
            for table in tables_to_check:
                # DuckDB count
                try:
                    duck_res = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()  # nosec B608
                    duck_count = duck_res[0] if duck_res else 0
                except Exception as e:
                    return ValidationResult(
                        is_valid=False,
                        discrepancy_reason=f"Table {table} query failed in DuckDB: {e}",
                    )

                # Aurora count
                try:
                    pg_cursor.execute(f'SELECT count(*) FROM "{table}"')  # nosec B608
                    pg_res = pg_cursor.fetchone()
                    pg_count = pg_res[0] if pg_res else 0
                except Exception as e:
                    logger.warning("Failed to query table %s in Aurora: %s", table, e)
                    continue  # If Aurora query fails temporarily, don't trigger full re-init for network blip

                # Strict row count comparison
                if duck_count != pg_count:
                    logger.warning(
                        "Row count drift detected on %s: DuckDB=%d vs Aurora=%d",
                        table, duck_count, pg_count
                    )
                    return ValidationResult(
                        is_valid=False,
                        discrepancy_reason=(
                            f"Row count mismatch on '{table}': DuckDB={duck_count} vs Aurora={pg_count}"
                        ),
                        details={"table": table, "duckdb_rows": duck_count, "aurora_rows": pg_count},
                    )

            return ValidationResult(is_valid=True)
        finally:
            if owns_conn:
                conn.close()
