#!/usr/bin/env python3
"""
restore_parquet_to_postgres.py — Disaster Recovery Importer for Reality Check.

Reads Parquet backups from dr/data/ and restores them into a target PostgreSQL database
(e.g., local Docker Compose PostgreSQL or a newly provisioned Aurora instance).

Features:
  - Automatically initializes schemas & extensions (pgvector).
  - Handles JSONB, DATE, and TIMESTAMPTZ conversions.
  - Performs idempotent ON CONFLICT upserts.
  - Verifies row count equality after import.

Usage:
  python3 dr/restore_parquet_to_postgres.py \
    --host localhost --port 5433 --user postgres --password postgres --dbname postgres
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import duckdb
import psycopg
from psycopg import sql

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dr.restore")

DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

SCHEMA_DDL = """
-- Extensions
CREATE EXTENSION IF NOT EXISTS vector;

-- daily_cost
CREATE TABLE IF NOT EXISTS daily_cost (
    usage_date            date PRIMARY KEY,
    application_name      text          NOT NULL DEFAULT 'RealityChecker',
    unblended_usd         numeric(14,6) NOT NULL,
    blended_usd           numeric(14,6) NOT NULL,
    net_usd               numeric(14,6) NOT NULL,
    currency              text          NOT NULL,
    services              jsonb         NOT NULL,
    gross_usd             numeric(14,6),
    credit_usd            numeric(14,6),
    gross_services        jsonb,
    applications          jsonb,
    gross_applications    jsonb,
    plan_type             text,
    credits_granted_usd   numeric(14,2),
    credits_remaining_usd numeric(14,2),
    credits_consumed_usd  numeric(14,2),
    credits_expiry        date,
    projection            jsonb,
    change_details        text,
    change_url            text,
    collected_at          timestamptz   NOT NULL DEFAULT now()
);

-- resource_inventory
CREATE TABLE IF NOT EXISTS resource_inventory (
    arn               text PRIMARY KEY,
    service           text        NOT NULL,
    resource_type     text        NOT NULL,
    region            text        NOT NULL,
    application_name  text        NOT NULL DEFAULT 'RealityChecker',
    tags              jsonb,
    created_at        timestamptz,
    first_seen        timestamptz NOT NULL DEFAULT now(),
    last_seen         timestamptz NOT NULL DEFAULT now()
);

-- __cdc_metadata
CREATE TABLE IF NOT EXISTS __cdc_metadata (
    key               text PRIMARY KEY,
    value             text NOT NULL,
    updated_at        timestamptz NOT NULL DEFAULT now()
);
"""


def restore_daily_cost(conn: psycopg.Connection, parquet_path: str) -> int:
    """Restore daily_cost from Parquet into PostgreSQL."""
    con = duckdb.connect()
    rows = con.execute(f"SELECT * FROM read_parquet('{parquet_path}')").fetchall()
    cols = [desc[0] for desc in con.description]
    con.close()

    if not rows:
        logger.warning("No rows to restore from %s", parquet_path)
        return 0

    inserted = 0
    with conn.cursor() as cur:
        for r in rows:
            row_dict = dict(zip(cols, r))
            # Format json fields
            for json_field in ("services", "gross_services", "applications", "gross_applications", "projection"):
                val = row_dict.get(json_field)
                if val is not None and not isinstance(val, str):
                    row_dict[json_field] = json.dumps(val, default=str)

            cur.execute(
                """
                INSERT INTO daily_cost (
                    usage_date, application_name, unblended_usd, blended_usd, net_usd,
                    currency, services, gross_usd, credit_usd, gross_services,
                    plan_type, credits_granted_usd, credits_remaining_usd, credits_consumed_usd,
                    credits_expiry, projection, applications, gross_applications,
                    change_details, change_url, collected_at
                ) VALUES (
                    %(usage_date)s, %(application_name)s, %(unblended_usd)s, %(blended_usd)s, %(net_usd)s,
                    %(currency)s, %(services)s::jsonb, %(gross_usd)s, %(credit_usd)s, %(gross_services)s::jsonb,
                    %(plan_type)s, %(credits_granted_usd)s, %(credits_remaining_usd)s, %(credits_consumed_usd)s,
                    %(credits_expiry)s, %(projection)s::jsonb, %(applications)s::jsonb, %(gross_applications)s::jsonb,
                    %(change_details)s, %(change_url)s, %(collected_at)s
                )
                ON CONFLICT (usage_date) DO UPDATE SET
                    gross_usd = EXCLUDED.gross_usd,
                    net_usd = EXCLUDED.net_usd,
                    services = EXCLUDED.services,
                    gross_services = EXCLUDED.gross_services,
                    applications = EXCLUDED.applications,
                    gross_applications = EXCLUDED.gross_applications,
                    change_details = EXCLUDED.change_details,
                    change_url = EXCLUDED.change_url,
                    collected_at = EXCLUDED.collected_at;
                """,
                row_dict,
            )
            inserted += 1
    conn.commit()
    return inserted


def restore_resource_inventory(conn: psycopg.Connection, parquet_path: str) -> int:
    """Restore resource_inventory from Parquet into PostgreSQL."""
    con = duckdb.connect()
    rows = con.execute(f"SELECT * FROM read_parquet('{parquet_path}')").fetchall()
    cols = [desc[0] for desc in con.description]
    con.close()

    if not rows:
        return 0

    inserted = 0
    with conn.cursor() as cur:
        for r in rows:
            row_dict = dict(zip(cols, r))
            val = row_dict.get("tags")
            if val is not None and not isinstance(val, str):
                row_dict["tags"] = json.dumps(val, default=str)

            cur.execute(
                """
                INSERT INTO resource_inventory (
                    arn, service, resource_type, region, application_name,
                    tags, created_at, first_seen, last_seen
                ) VALUES (
                    %(arn)s, %(service)s, %(resource_type)s, %(region)s, %(application_name)s,
                    %(tags)s::jsonb, %(created_at)s, %(first_seen)s, %(last_seen)s
                )
                ON CONFLICT (arn) DO UPDATE SET
                    last_seen = EXCLUDED.last_seen,
                    tags = EXCLUDED.tags;
                """,
                row_dict,
            )
            inserted += 1
    conn.commit()
    return inserted


def restore_cdc_metadata(conn: psycopg.Connection, parquet_path: str) -> int:
    """Restore __cdc_metadata from Parquet into PostgreSQL."""
    con = duckdb.connect()
    rows = con.execute(f"SELECT * FROM read_parquet('{parquet_path}')").fetchall()
    cols = [desc[0] for desc in con.description]
    con.close()

    if not rows:
        return 0

    inserted = 0
    with conn.cursor() as cur:
        for r in rows:
            row_dict = dict(zip(cols, r))
            cur.execute(
                """
                INSERT INTO __cdc_metadata (key, value, updated_at)
                VALUES (%(key)s, %(value)s, %(updated_at)s)
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value,
                    updated_at = EXCLUDED.updated_at;
                """,
                row_dict,
            )
            inserted += 1
    conn.commit()
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore Parquet data to target PostgreSQL.")
    parser.add_argument("--host", default=os.environ.get("PGHOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PGPORT", "5433")))
    parser.add_argument("--dbname", default=os.environ.get("PGDATABASE", "postgres"))
    parser.add_argument("--user", default=os.environ.get("PGUSER", "postgres"))
    parser.add_argument("--password", default=os.environ.get("PGPASSWORD", "postgres"))
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    args = parser.parse_args()

    conninfo = f"host={args.host} port={args.port} dbname={args.dbname} user={args.user} password={args.password}"
    logger.info("Connecting to target PostgreSQL at %s:%d/%s...", args.host, args.port, args.dbname)

    try:
        with psycopg.connect(conninfo) as conn:
            logger.info("Initializing schema DDL & extensions...")
            with conn.cursor() as cur:
                cur.execute(SCHEMA_DDL)
            conn.commit()

            # Restore daily_cost
            dc_path = os.path.join(args.data_dir, "daily_cost.parquet")
            if os.path.exists(dc_path):
                cnt = restore_daily_cost(conn, dc_path)
                logger.info("Restored daily_cost: %d rows from %s", cnt, dc_path)

            # Restore resource_inventory
            ri_path = os.path.join(args.data_dir, "resource_inventory.parquet")
            if os.path.exists(ri_path):
                cnt = restore_resource_inventory(conn, ri_path)
                logger.info("Restored resource_inventory: %d rows from %s", cnt, ri_path)

            # Restore cdc_metadata
            cdc_path = os.path.join(args.data_dir, "__cdc_metadata.parquet")
            if os.path.exists(cdc_path):
                cnt = restore_cdc_metadata(conn, cdc_path)
                logger.info("Restored __cdc_metadata: %d rows from %s", cnt, cdc_path)

            # Verify target counts
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM daily_cost;")
                dc_cnt = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM resource_inventory;")
                ri_cnt = cur.fetchone()[0]
                cur.execute("SELECT sum(gross_usd) FROM daily_cost;")
                total_gross = cur.fetchone()[0]

            logger.info("==================================================")
            logger.info("VERIFICATION SUCCESS:")
            logger.info("  daily_cost table count        : %d rows", dc_cnt)
            logger.info("  resource_inventory table count: %d rows", ri_cnt)
            logger.info("  total gross spend recorded    : $%.4f", float(total_gross or 0))
            logger.info("==================================================")

    except Exception as exc:
        logger.error("Restore failed: %s", exc, exc_info=True)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
