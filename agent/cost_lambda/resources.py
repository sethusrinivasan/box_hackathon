"""
resources.py — active AWS resource inventory for the cost tracker.

Enumerates the resources that actually exist on the account so the cost view can
answer "what is running, since when, and for how long" — not just "what did it
cost". Designed to be CHEAP and BOUNDED:

  * One paginated Resource Groups Tagging API sweep lists every taggable
    resource across services in the region in a handful of calls (free).
  * Targeted describe calls fill in CREATION TIMES for the known cost drivers
    (Lambda, RDS, S3) because the tagging API does not expose them.

We deliberately do NOT fan out to every service API on the account: that is
unbounded, slow, and (for Cost Explorer) billed per call. The tagging sweep plus
a few describes covers everything this project provisions and the common cost
drivers, while keeping the run fast and nearly free.

Each resource record carries:
  arn, service, resource_type, region, created_at (when discoverable),
  first_seen (when THIS tracker first observed it), last_seen (this run).
The collector upserts these so first_seen/last_seen accumulate a running-time
history across runs even for resources whose creation time AWS does not expose.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re

import boto3

log = logging.getLogger("cost_collector.resources")

REGION = os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_REGION", "us-east-1")


def _service_and_type(arn: str) -> tuple[str, str]:
    """Parse 'arn:partition:service:region:acct:restype/resid' into
    (service, resource_type). Falls back gracefully for odd ARN shapes."""
    parts = arn.split(":", 5)
    service = parts[2] if len(parts) > 2 else "unknown"
    tail = parts[5] if len(parts) > 5 else ""
    # resource type is the segment before the first '/' or ':' in the tail
    m = re.split(r"[/:]", tail, maxsplit=1)
    rtype = m[0] if m and m[0] else service
    return service, rtype


def _tagging_sweep(client) -> dict[str, dict]:
    """Return {arn: base-record} for every taggable resource in the region."""
    found: dict[str, dict] = {}
    paginator = client.get_paginator("get_resources")
    for page in paginator.paginate(ResourcesPerPage=100):
        for item in page.get("ResourceTagMappingList", []):
            arn = item["ResourceARN"]
            service, rtype = _service_and_type(arn)
            found[arn] = {
                "arn": arn,
                "service": service,
                "resource_type": rtype,
                "region": REGION,
                "created_at": None,  # filled by describers where available
            }
    return found


def _iso(ts) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, dt.datetime):
        return ts.astimezone(dt.timezone.utc).isoformat()
    return str(ts)


def _enrich_lambda(records: dict[str, dict]) -> None:
    """Fill creation (LastModified) for Lambda functions."""
    try:
        client = boto3.client("lambda", region_name=REGION)
        paginator = client.get_paginator("list_functions")
        by_arn = {}
        for page in paginator.paginate():
            for fn in page.get("Functions", []):
                by_arn[fn["FunctionArn"]] = fn.get("LastModified")
        for arn, rec in records.items():
            if rec["service"] == "lambda" and arn in by_arn:
                rec["created_at"] = _iso(by_arn[arn])
    except Exception as exc:  # inventory is best-effort; never fail the run
        log.warning("lambda enrich failed: %s", exc)


def _enrich_rds(records: dict[str, dict]) -> None:
    """Fill ClusterCreateTime for Aurora/RDS clusters (and instances)."""
    try:
        client = boto3.client("rds", region_name=REGION)
        for page in client.get_paginator("describe_db_clusters").paginate():
            for c in page.get("DBClusters", []):
                arn = c.get("DBClusterArn")
                if arn:
                    records.setdefault(arn, {
                        "arn": arn, "service": "rds",
                        "resource_type": "cluster", "region": REGION,
                    })
                    records[arn]["created_at"] = _iso(c.get("ClusterCreateTime"))
        for page in client.get_paginator("describe_db_instances").paginate():
            for i in page.get("DBInstances", []):
                arn = i.get("DBInstanceArn")
                if arn:
                    records.setdefault(arn, {
                        "arn": arn, "service": "rds",
                        "resource_type": "instance", "region": REGION,
                    })
                    records[arn]["created_at"] = _iso(i.get("InstanceCreateTime"))
    except Exception as exc:
        log.warning("rds enrich failed: %s", exc)


def _enrich_s3(records: dict[str, dict]) -> None:
    """Fill CreationDate for S3 buckets (global list, filtered to our ARNs)."""
    try:
        client = boto3.client("s3", region_name=REGION)
        created = {b["Name"]: b.get("CreationDate") for b in
                   client.list_buckets().get("Buckets", [])}
        for arn, rec in records.items():
            if rec["service"] == "s3":
                name = arn.split(":::")[-1].split("/")[0]
                if name in created:
                    rec["created_at"] = _iso(created[name])
    except Exception as exc:
        log.warning("s3 enrich failed: %s", exc)


def inventory() -> list[dict]:
    """Enumerate active resources with creation times where discoverable.

    Returns a list of records. Cheap: one tagging sweep + three describe
    passes. Best-effort — a failure in any enricher degrades that field to
    null rather than failing the whole collection run.
    """
    client = boto3.client("resourcegroupstaggingapi", region_name=REGION)
    records = _tagging_sweep(client)
    _enrich_lambda(records)
    _enrich_rds(records)
    _enrich_s3(records)
    out = sorted(records.values(), key=lambda r: (r["service"], r["arn"]))
    log.info("resource inventory: %d resources", len(out))
    return out
