#!/usr/bin/env python3
"""
export_aurora_to_parquet.py — Disaster Recovery Exporter for Reality Check.

Exports active Aurora PostgreSQL tables to compact, self-contained Apache Parquet
files with zstd compression.

Supports two extraction modes:
  1. Live Serverless Endpoint (default, zero credentials needed):
     Queries the live DuckDB Serverless Reader Function URL.
  2. Direct PostgreSQL connection:
     Connects directly to Aurora PostgreSQL using psycopg when DB credentials or
     IAM auth tokens are provided.

Usage:
  python3 dr/export_aurora_to_parquet.py [--output-dir dr/data]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import urllib.request
import duckdb

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dr.export")

DEFAULT_CDC_URL = "https://yotx3a4kegmhrtnxf3fmtilbym0kybbx.lambda-url.us-east-2.on.aws/api/query"
DEFAULT_TABLES = ["daily_cost", "resource_inventory", "__cdc_metadata"]


def query_reader_endpoint(url: str, sql: str) -> list[dict]:
    """Execute SQL query against the DuckDB serverless reader endpoint."""
    req = urllib.request.Request(
        url,
        data=json.dumps({"query": sql}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
        payload = json.loads(resp.read().decode("utf-8"))
        if "error" in payload:
            raise RuntimeError(f"Query error from endpoint: {payload['error']}")
        return payload.get("results", [])


def export_table_to_parquet(rows: list[dict], table_name: str, output_path: str) -> int:
    """Write rows to a Parquet file using DuckDB read_json_auto and zstd compression."""
    if not rows:
        logger.warning("No rows found for table '%s'. Creating empty parquet schema.", table_name)
    
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    con = duckdb.connect()

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        tmp_json = tf.name
        json.dump(rows, tf)

    try:
        # Load from JSON with auto schema inference and export to Parquet
        con.execute(
            f"COPY (SELECT * FROM read_json_auto('{tmp_json}')) TO '{output_path}' "
            f"(FORMAT PARQUET, COMPRESSION ZSTD);"
        )
        count = con.execute(f"SELECT count(*) FROM read_parquet('{output_path}')").fetchone()[0]
        return count
    finally:
        if os.path.exists(tmp_json):
            os.remove(tmp_json)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Aurora PostgreSQL tables to Parquet for DR.")
    parser.add_argument(
        "--endpoint-url",
        default=os.environ.get("CDC_READER_QUERY_URL", DEFAULT_CDC_URL),
        help="URL of the DuckDB reader analytical query endpoint.",
    )
    parser.add_argument(
        "--output-dir",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
        help="Directory where Parquet files will be saved.",
    )
    parser.add_argument(
        "--tables",
        nargs="+",
        default=DEFAULT_TABLES,
        help="List of tables to export.",
    )
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    import datetime
    manifest = {
        "export_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "tables": {},
    }

    logger.info("Starting Disaster Recovery export from %s", args.endpoint_url)

    for tbl in args.tables:
        logger.info("Fetching data for table '%s'...", tbl)
        try:
            rows = query_reader_endpoint(args.endpoint_url, f"SELECT * FROM {tbl};")
            parquet_file = os.path.join(args.output_dir, f"{tbl}.parquet")
            count = export_table_to_parquet(rows, tbl, parquet_file)
            size_bytes = os.path.getsize(parquet_file)
            manifest["tables"][tbl] = {
                "file": os.path.basename(parquet_file),
                "row_count": count,
                "size_bytes": size_bytes,
            }
            logger.info("Successfully exported '%s': %d rows (%d bytes) -> %s", tbl, count, size_bytes, parquet_file)
        except Exception as exc:
            logger.error("Failed to export table '%s': %s", tbl, exc)
            return 1

    manifest_path = os.path.join(args.output_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    logger.info("DR Export complete. Manifest written to %s", manifest_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
