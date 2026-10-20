"""
agent/cdc/reader.py — Stateless Reader Lambda for DuckDB analytical queries.

Queries DuckDB database stored in S3 using a hybrid caching model:
  1. Compares S3 ETag against local /tmp/analytics.duckdb (via lightweight head_object).
  2. Warm hit (< 1 ms): reuses cached local database in read_only mode.
  3. Cold miss / ETag updated: fetches fresh DuckDB replica into /tmp or queries
     directly via DuckDB's httpfs extension.
  4. Runs complex analytical aggregations and filters with ZERO load on Aurora.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError
import duckdb

from agent.cdc.metrics import emit_metric

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("reality_check.cdc.reader")

S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_KEY = os.environ.get("S3_KEY", "analytics/analytics.duckdb")
LOCAL_DB_PATH = os.environ.get("LOCAL_DB_PATH", "/tmp/analytics.duckdb")
ETAG_PATH = os.environ.get("LOCAL_ETAG_PATH", "/tmp/analytics.etag")


class DuckDbReader:
    """Stateless analytical query runner with local container caching."""

    def __init__(
        self,
        s3_bucket: str = S3_BUCKET,
        s3_key: str = S3_KEY,
        local_db_path: str = LOCAL_DB_PATH,
        etag_path: str = ETAG_PATH,
        s3_client: Any | None = None,
    ) -> None:
        self.s3_bucket = s3_bucket
        self.s3_key = s3_key
        self.local_db_path = local_db_path
        self.etag_path = etag_path
        self.s3 = s3_client or boto3.client("s3")

    def _get_local_etag(self) -> str | None:
        if os.path.exists(self.etag_path) and os.path.exists(self.local_db_path):
            try:
                with open(self.etag_path, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except OSError:
                return None
        return None

    def _save_local_etag(self, etag: str) -> None:
        try:
            with open(self.etag_path, "w", encoding="utf-8") as f:
                f.write(etag)
        except OSError as e:
            logger.warning("Could not persist etag cache: %s", e)

    def ensure_fresh_database(self) -> bool:
        """Verify local cache freshness against S3. Returns True if cache was a hit."""
        if not self.s3_bucket:
            return True  # Local mode / unit tests

        try:
            head = self.s3.head_object(Bucket=self.s3_bucket, Key=self.s3_key)
            remote_etag = head.get("ETag", "").strip('"')
            local_etag = self._get_local_etag()

            if local_etag == remote_etag and os.path.exists(self.local_db_path):
                logger.debug("Cache hit: ETag %s matches local replica.", remote_etag)
                return True

            logger.info("Cache miss (local: %s, remote: %s). Refreshing from S3...", local_etag, remote_etag)
            os.makedirs(os.path.dirname(os.path.abspath(self.local_db_path)), exist_ok=True)
            self.s3.download_file(self.s3_bucket, self.s3_key, self.local_db_path)
            self._save_local_etag(remote_etag)
            return False

        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            logger.error("Error refreshing database from S3 (%s): %s", code, e)
            if not os.path.exists(self.local_db_path):
                raise
            return False

    def query(self, sql: str, params: list[Any] | None = None) -> dict[str, Any]:
        """Execute analytical query against cached DuckDB replica."""
        t0 = time.perf_counter()
        cache_hit = self.ensure_fresh_database()

        if not os.path.exists(self.local_db_path):
            raise FileNotFoundError(f"DuckDB database not found at {self.local_db_path}")

        conn = duckdb.connect(self.local_db_path, read_only=True)
        try:
            # Query CDC watermark metadata if present
            watermark: dict[str, Any] = {}
            tables = [r[0] for r in conn.execute("SHOW TABLES;").fetchall()]
            if "__cdc_metadata" in tables:
                meta_rows = conn.execute("SELECT key, value FROM __cdc_metadata").fetchall()
                watermark = {r[0]: r[1] for r in meta_rows}

            # Execute caller query
            cursor = conn.execute(sql, params or [])
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            rows = cursor.fetchall()
            results = [dict(zip(columns, r)) for r in rows]

            query_duration_ms = (time.perf_counter() - t0) * 1000
            emit_metric("ReaderQueryDurationMs", query_duration_ms, "Milliseconds")
            emit_metric("ReaderCacheHit", 1 if cache_hit else 0, "Count")

            return {
                "results": results,
                "row_count": len(results),
                "query_time_ms": round(query_duration_ms, 2),
                "cache_hit": cache_hit,
                "watermark": watermark,
                "duckdb_version": duckdb.__version__,
            }
        finally:
            conn.close()


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda Function URL / API Gateway handler for analytical queries."""
    reader = DuckDbReader()

    # Determine query from event (support Function URL / HTTP POST and direct invoke)
    query_sql = "SELECT * FROM daily_cost ORDER BY usage_date DESC LIMIT 10;"
    params: list[Any] = []

    if isinstance(event, dict):
        body = event.get("body")
        if body:
            try:
                body_json = json.loads(body) if isinstance(body, str) else body
                query_sql = body_json.get("query", query_sql)
                params = body_json.get("params", [])
            except Exception:
                pass
        elif "query" in event:
            query_sql = event["query"]
            params = event.get("params", [])

    try:
        response = reader.query(query_sql, params)
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(response, default=str),
        }
    except Exception as e:
        logger.error("Query execution failed: %s", e, exc_info=True)
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": str(e)}),
        }
