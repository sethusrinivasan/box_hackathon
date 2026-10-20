"""
Daily holistic AWS cost + credit + resource-inventory collector.

Runs on a schedule (EventBridge Scheduler). For the previous UTC day it:
  * pulls GROSS (pre-credit) spend, net spend, and the credit offset from Cost
    Explorer — gross is the real consumption the console shows; net collapses to
    ~$0 while promotional credits cover usage (see billing.py),
  * snapshots the credit balance + a runway projection (exhaustion vs. expiry),
  * enumerates active AWS resources with creation/first-seen/last-seen times,
and upserts all of it into Aurora Serverless v2 PostgreSQL.

Connectivity rules (hard requirements):
  * NATIVE PostgreSQL wire protocol via psycopg. No RDS Data API.
  * IAM DATABASE AUTHENTICATION only. We mint a short-lived IAM auth token with
    boto3 (rds.generate_db_auth_token) and pass it as the password. There is NO
    stored password and NO Secrets Manager anywhere in this path.
  * TLS required (verify-full); IAM auth mandates an encrypted connection.
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

import billing
import resources
from metrics import emit_metric

log = logging.getLogger("cost_collector")
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ["DB_NAME"]
DB_IAM_USER = os.environ["DB_IAM_USER"]
REGION = os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_REGION", "us-east-1")

# Trailing window used to average gross burn for the credit-runway projection.
BURN_WINDOW_DAYS = int(os.environ.get("BURN_WINDOW_DAYS", "7"))

_rds = boto3.client("rds", region_name=REGION)
_ce = boto3.client("ce", region_name=REGION)


# --------------------------------------------------------------------------- #
# Cost Explorer
# --------------------------------------------------------------------------- #
def _day_window(day: str | None = None) -> tuple[str, str]:
    """(start, end) for a UTC day; CE End is exclusive. Default: yesterday."""
    if day is None:
        start = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    else:
        start = dt.date.fromisoformat(day)
    return start.isoformat(), (start + dt.timedelta(days=1)).isoformat()


def _gross_services(start: str, end: str) -> dict:
    """Per-service GROSS (pre-credit) spend for the day. This is what the
    console's cost breakdown shows; plain UnblendedCost nets to ~$0 on a
    credit-covered account and hides it."""
    svc = _ce.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        Filter={"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}},
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    groups = svc["ResultsByTime"][0]["Groups"]
    return {
        g["Keys"][0]: round(float(g["Metrics"]["UnblendedCost"]["Amount"]), 6)
        for g in groups
        if float(g["Metrics"]["UnblendedCost"]["Amount"]) != 0.0
    }


def _gross_applications(start: str, end: str) -> dict:
    """Per-application GROSS spend for the day based on ApplicationName tag.
    Groups by tag ApplicationName from Cost Explorer; falls back to attributing
    all project gross spend to 'RealityChecker' if tags have not yet settled in CE."""
    try:
        res = _ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}},
            GroupBy=[{"Type": "TAG", "Key": "ApplicationName"}],
        )
        groups = res.get("ResultsByTime", [{}])[0].get("Groups", [])
        apps: dict[str, float] = {}
        for g in groups:
            raw_key = g["Keys"][0]
            val = raw_key.split("$", 1)[1] if "$" in raw_key else raw_key
            amt = round(float(g["Metrics"]["UnblendedCost"]["Amount"]), 6)
            if amt != 0.0:
                target_key = val if val else "RealityChecker"
                apps[target_key] = round(apps.get(target_key, 0.0) + amt, 6)
        return apps
    except Exception as exc:
        log.warning("Cost Explorer application tag group failed: %s", exc)
        return {}


def collect(avg_daily_gross: float, day: str | None = None) -> dict:
    """Holistic snapshot for a UTC day: gross/net/credit totals, per-service
    gross breakdown, and the credit balance + runway projection."""
    start, end = _day_window(day)
    bill = billing.harvest(start, end, avg_daily_gross)
    services = _gross_services(start, end)
    applications = _gross_applications(start, end)
    if not applications and services:
        applications = {"RealityChecker": round(sum(services.values()), 6)}
    elif "RealityChecker" not in applications and services:
        applications["RealityChecker"] = round(sum(applications.values()) or sum(services.values()), 6)

    return {
        "usage_date": start,
        "application_name": "RealityChecker",
        "gross": bill["day_gross_usd"],
        "credit": bill["day_credit_usd"],
        "net": bill["day_net_usd"],
        "net_unblended": bill["day_net_unblended_usd"],
        "currency": "USD",
        "services": services,                       # gross, per service
        "applications": applications,               # gross, per ApplicationName tag
        "plan_type": bill["plan_type"],
        "credits_granted": bill["credits_granted_usd"],
        "credits_remaining": bill["credits_remaining_usd"],
        "credits_consumed": bill["credits_consumed_usd"],
        "credits_expiry": bill["credits_expiry"],
        "projection": bill["projection"],
        "free_tier_usage": bill["free_tier_usage"],
    }


# --------------------------------------------------------------------------- #
# Aurora via native PG + IAM auth
# --------------------------------------------------------------------------- #
def _connect() -> psycopg.Connection:
    """Open a native PostgreSQL connection using a freshly-minted IAM auth token
    as the password. No secret is stored; the token is valid ~15 minutes.

    Aurora Serverless v2 auto-pauses when idle and can take ~15s to resume on the
    first connection, so we retry once (with a fresh token) to ride through a
    cold resume rather than failing the run.

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


# NOTE: the schema (tables, columns, grants) is owned by the admin bootstrap
# user via agent/cost_lambda/schema.sql — run it ONCE at setup. The collector
# connects as the least-privilege `cost_collector` role, which can INSERT/UPDATE
# rows but is NOT the table owner and therefore cannot CREATE/ALTER tables. So
# we deliberately do NO schema mutation at runtime; attempting it would (rightly)
# fail with "must be owner of table". If a column/table is missing, that's a
# signal to re-run schema.sql, not something the runtime role should paper over.

_UPSERT_COST = """
INSERT INTO daily_cost
    (usage_date, application_name, unblended_usd, blended_usd, net_usd, currency, services,
     gross_usd, credit_usd, gross_services, applications, gross_applications, plan_type,
     credits_granted_usd, credits_remaining_usd, credits_consumed_usd, credits_expiry,
     projection, collected_at)
VALUES
    (%(usage_date)s, %(application_name)s, %(net)s, %(net)s, %(net)s, %(currency)s, %(services)s,
     %(gross)s, %(credit)s, %(services)s, %(applications)s, %(applications)s, %(plan_type)s,
     %(credits_granted)s, %(credits_remaining)s, %(credits_consumed)s, %(credits_expiry)s,
     %(projection)s, now())
ON CONFLICT (usage_date) DO UPDATE SET
    application_name      = EXCLUDED.application_name,
    unblended_usd         = EXCLUDED.unblended_usd,
    blended_usd           = EXCLUDED.blended_usd,
    net_usd               = EXCLUDED.net_usd,
    currency              = EXCLUDED.currency,
    services              = EXCLUDED.services,
    gross_usd             = EXCLUDED.gross_usd,
    credit_usd            = EXCLUDED.credit_usd,
    gross_services        = EXCLUDED.gross_services,
    applications          = EXCLUDED.applications,
    gross_applications    = EXCLUDED.gross_applications,
    plan_type             = EXCLUDED.plan_type,
    credits_granted_usd   = EXCLUDED.credits_granted_usd,
    credits_remaining_usd = EXCLUDED.credits_remaining_usd,
    credits_consumed_usd  = EXCLUDED.credits_consumed_usd,
    credits_expiry        = EXCLUDED.credits_expiry,
    projection            = EXCLUDED.projection,
    collected_at          = now();
"""

# Insert new resources; on repeat, bump last_seen and fill created_at if we
# learned it this run. first_seen is preserved (set once on first insert).
_UPSERT_RESOURCE = """
INSERT INTO resource_inventory
    (arn, service, resource_type, region, application_name, tags, created_at, first_seen, last_seen)
VALUES
    (%(arn)s, %(service)s, %(resource_type)s, %(region)s, %(application_name)s, %(tags)s, %(created_at)s, now(), now())
ON CONFLICT (arn) DO UPDATE SET
    service          = EXCLUDED.service,
    resource_type    = EXCLUDED.resource_type,
    region           = EXCLUDED.region,
    application_name = EXCLUDED.application_name,
    tags             = COALESCE(EXCLUDED.tags, resource_inventory.tags),
    created_at       = COALESCE(resource_inventory.created_at, EXCLUDED.created_at),
    last_seen        = now();
"""


def _avg_daily_gross(cur) -> float:
    """Average gross spend/day over the trailing window from stored history.
    Smooths Cost Explorer's settling lag (usage posts a few days late). Returns
    0.0 when there's no history yet (first run)."""
    cutoff = (dt.datetime.now(dt.timezone.utc).date()
              - dt.timedelta(days=BURN_WINDOW_DAYS)).isoformat()
    cur.execute(
        "SELECT COALESCE(AVG(gross_usd), 0) FROM daily_cost "
        "WHERE usage_date >= %s AND gross_usd IS NOT NULL;", (cutoff,))
    row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def run(day: str | None = None) -> dict:
    """Full collection cycle against the DB: ensure schema, compute the trailing
    burn average, harvest billing + inventory, and upsert everything."""
    inv = resources.inventory()
    with _connect() as conn:
        with conn.cursor() as cur:
            avg = _avg_daily_gross(cur)
            snapshot = collect(avg, day)
            params = dict(snapshot)
            params["services"] = json.dumps(snapshot["services"])
            params["applications"] = json.dumps(snapshot.get("applications", {"RealityChecker": snapshot["gross"]}))
            params["projection"] = json.dumps(snapshot["projection"])
            cur.execute(_UPSERT_COST, params)
            for r in inv:
                cur.execute(_UPSERT_RESOURCE, r)
        conn.commit()
    snapshot["resource_count"] = len(inv)
    return snapshot


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #
def lambda_handler(event, context=None):
    # Allow a manual backfill: {"usage_date": "YYYY-MM-DD"}.
    day = (event or {}).get("usage_date") if isinstance(event, dict) else None
    snapshot = run(day)
    proj = snapshot.get("projection") or {}
    log.info(json.dumps({
        "msg": "cost.persisted",
        "usage_date": snapshot["usage_date"],
        "gross_usd": snapshot["gross"],
        "net_usd": snapshot["net"],
        "credits_remaining_usd": snapshot["credits_remaining"],
        "binding_limit": proj.get("binding_limit"),
        "days_until_exhaustion": proj.get("days_until_exhaustion"),
        "days_until_expiry": proj.get("days_until_expiry"),
        "services": len(snapshot["services"]),
        "resources": snapshot["resource_count"],
    }, default=str))
    return {
        "ok": True,
        "usage_date": snapshot["usage_date"],
        "gross_usd": snapshot["gross"],
        "net_usd": snapshot["net"],
        "credits_remaining_usd": snapshot["credits_remaining"],
        "service_count": len(snapshot["services"]),
        "resource_count": snapshot["resource_count"],
        "projection": proj,
    }
