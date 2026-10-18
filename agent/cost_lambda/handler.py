"""
Daily holistic AWS cost collector.

Runs once a day (EventBridge Scheduler). Pulls the previous UTC day's cost from
Cost Explorer — total plus a per-service breakdown and the blended/unblended/net
(post-credit) amounts — and upserts a durable record into Aurora Serverless v2
PostgreSQL.

Connectivity rules (hard requirements):
  * NATIVE PostgreSQL wire protocol via psycopg. No RDS Data API.
  * IAM DATABASE AUTHENTICATION only. We mint a short-lived IAM auth token with
    boto3 (rds.generate_db_auth_token) and pass it as the password. There is NO
    stored password and NO Secrets Manager anywhere in this path.
  * TLS required (sslmode=require); IAM auth mandates an encrypted connection.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time

import boto3
import certifi
import psycopg

from metrics import emit_metric

log = logging.getLogger("cost_collector")
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ["DB_NAME"]
DB_IAM_USER = os.environ["DB_IAM_USER"]
REGION = os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_REGION", "us-east-1")

_rds = boto3.client("rds", region_name=REGION)
_ce = boto3.client("ce", region_name=REGION)


# --------------------------------------------------------------------------- #
# Cost Explorer
# --------------------------------------------------------------------------- #
def _yesterday_utc() -> tuple[str, str]:
    today = dt.datetime.now(dt.timezone.utc).date()
    start = today - dt.timedelta(days=1)
    return start.isoformat(), today.isoformat()  # CE End is exclusive


def collect_costs() -> dict:
    """Return a holistic snapshot for the previous UTC day."""
    start, end = _yesterday_utc()

    # Totals with multiple metrics so we see gross vs. post-credit (net) spend.
    totals = _ce.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="DAILY",
        Metrics=["UnblendedCost", "BlendedCost", "NetUnblendedCost", "UsageQuantity"],
    )
    by_time = totals["ResultsByTime"][0]["Total"]

    # Per-service breakdown (unblended), so the record is genuinely holistic.
    svc = _ce.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    services = {
        g["Keys"][0]: float(g["Metrics"]["UnblendedCost"]["Amount"])
        for g in svc["ResultsByTime"][0]["Groups"]
        if float(g["Metrics"]["UnblendedCost"]["Amount"]) != 0.0
    }

    return {
        "usage_date": start,
        "unblended": float(by_time["UnblendedCost"]["Amount"]),
        "blended": float(by_time["BlendedCost"]["Amount"]),
        "net_unblended": float(by_time["NetUnblendedCost"]["Amount"]),
        "currency": by_time["UnblendedCost"]["Unit"],
        "services": services,
    }


# --------------------------------------------------------------------------- #
# Aurora via native PG + IAM auth
# --------------------------------------------------------------------------- #
def _connect() -> psycopg.Connection:
    """Open a native PostgreSQL connection using a freshly-minted IAM auth token
    as the password. No secret is stored; the token is valid ~15 minutes.

    Aurora Serverless v2 auto-pauses when idle and can take ~15s to resume on the
    first connection, so we retry once (with a fresh token) to ride through a
    cold resume rather than failing the daily run.

    TLS: verify-full against the certifi public CA bundle. Aurora EXPRESS
    configuration's internet access gateway presents a PUBLICLY-trusted Amazon
    Trust Services certificate (not the RDS private CA), and psycopg[binary]
    vendors an OpenSSL that doesn't resolve the Lambda OS trust store, so we
    point sslrootcert at certifi's bundle (which has the Amazon Trust roots)."""
    last_exc: Exception | None = None
    started = time.perf_counter()
    for attempt in range(2):
        token = _rds.generate_db_auth_token(
            DBHostname=DB_HOST, Port=DB_PORT, DBUsername=DB_IAM_USER, Region=REGION
        )
        try:
            conn = psycopg.connect(
                host=DB_HOST,
                port=DB_PORT,
                dbname=DB_NAME,
                user=DB_IAM_USER,
                password=token,          # the IAM auth token, not a stored password
                sslmode="verify-full",
                sslrootcert=certifi.where(),
                connect_timeout=20,
            )
            # Instrumentation: total connect latency + whether a cold-resume
            # retry was needed. Lets the dashboard track Aurora resume behavior.
            elapsed_ms = (time.perf_counter() - started) * 1000
            emit_metric("DbConnectLatencyMs", round(elapsed_ms, 1), "Milliseconds",
                        fn="collector")
            emit_metric("DbColdResume", 1 if attempt > 0 else 0, "Count",
                        fn="collector")
            return conn
        except psycopg.OperationalError as exc:
            last_exc = exc  # Aurora likely still resuming; retry once
    emit_metric("DbConnectFailure", 1, "Count", fn="collector")
    raise last_exc  # type: ignore[misc]


_DDL = """
CREATE TABLE IF NOT EXISTS daily_cost (
    usage_date     date PRIMARY KEY,
    unblended_usd  numeric(14,6) NOT NULL,
    blended_usd    numeric(14,6) NOT NULL,
    net_usd        numeric(14,6) NOT NULL,
    currency       text          NOT NULL,
    services       jsonb         NOT NULL,
    collected_at   timestamptz   NOT NULL DEFAULT now()
);
"""

_UPSERT = """
INSERT INTO daily_cost
    (usage_date, unblended_usd, blended_usd, net_usd, currency, services, collected_at)
VALUES
    (%(usage_date)s, %(unblended)s, %(blended)s, %(net_unblended)s, %(currency)s,
     %(services)s, now())
ON CONFLICT (usage_date) DO UPDATE SET
    unblended_usd = EXCLUDED.unblended_usd,
    blended_usd   = EXCLUDED.blended_usd,
    net_usd       = EXCLUDED.net_usd,
    currency      = EXCLUDED.currency,
    services      = EXCLUDED.services,
    collected_at  = now();
"""


def persist(snapshot: dict) -> None:
    params = dict(snapshot)
    params["services"] = json.dumps(snapshot["services"])
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_DDL)           # idempotent; safe every run
            cur.execute(_UPSERT, params)
        conn.commit()


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #
def lambda_handler(event, context=None):
    snapshot = collect_costs()
    persist(snapshot)
    log.info(json.dumps({
        "msg": "cost.persisted",
        "usage_date": snapshot["usage_date"],
        "unblended_usd": snapshot["unblended"],
        "net_usd": snapshot["net_unblended"],
        "services": len(snapshot["services"]),
    }))
    return {
        "ok": True,
        "usage_date": snapshot["usage_date"],
        "unblended_usd": snapshot["unblended"],
        "net_usd": snapshot["net_unblended"],
        "service_count": len(snapshot["services"]),
    }
