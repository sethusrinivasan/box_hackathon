"""
CloudFormation custom-resource provider for an Aurora PostgreSQL EXPRESS
configuration cluster.

WHY THIS EXISTS (see docs/KNOWN_ISSUES.md KI-1 / GitHub issue #3): express
configuration has no CloudFormation property, so we drive the RDS API directly.
A plain AwsCustomResource can't do it reliably because:
  * createDBCluster returns BEFORE the endpoint is assigned, and
  * deleteDBCluster fails while the express-created instance still exists.

This Lambda is the `on_event_handler` for a `cr.Provider` (custom-resources
framework). It follows the framework contract: it simply RETURNS a dict with
PhysicalResourceId and Data; the framework signs and posts the CFN response and
handles async waiting. We do NOT post to the response URL ourselves.

Lifecycle:
  Create: createDBCluster(WithExpressConfiguration), poll until the endpoint is
          assigned, return DbClusterResourceId + Endpoint.
  Delete: delete member instance(s), wait, then delete the cluster (idempotent).
  Update: no-op; return current attributes.
"""
from __future__ import annotations

import time

import boto3

rds = boto3.client("rds")

_POLL_SECONDS = 10
_CREATE_TIMEOUT = 600
_DELETE_TIMEOUT = 600


def _wait_endpoint(cluster_id: str) -> dict:
    deadline = time.time() + _CREATE_TIMEOUT
    while time.time() < deadline:
        c = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
        if c.get("Endpoint"):
            return c
        time.sleep(_POLL_SECONDS)
    raise TimeoutError(f"endpoint not assigned for {cluster_id} in time")


# All CloudWatch log types Aurora PostgreSQL can export. Enabling every
# supported type gives full operational visibility (query/error logs via
# "postgresql", engine "instance" logs, and "iam-db-auth-error" so failed
# IAM-auth attempts are auditable). Harmless if a future engine version drops
# one — modify-db-cluster simply ignores unknown types we do not request.
_LOG_EXPORTS = ["postgresql", "instance", "iam-db-auth-error"]

# Purpose/classification tags. Beyond cost allocation, these back tag-scoped
# (ABAC) IAM on the management-plane actions (rds:ModifyDBCluster etc.), so the
# provider role can only act on clusters that carry them.
_TAGS = [
    {"Key": "ApplicationName", "Value": "RealityChecker"},
    {"Key": "Project", "Value": "RealityCheck"},
    {"Key": "Component", "Value": "cost-tracker"},
    {"Key": "DataClassification", "Value": "cost-metrics"},
]


def _on_create(cluster_id: str) -> dict:
    rds.create_db_cluster(
        DBClusterIdentifier=cluster_id,
        Engine="aurora-postgresql",
        WithExpressConfiguration=True,
        EnableCloudwatchLogsExports=_LOG_EXPORTS,
        Tags=_TAGS,
    )
    c = _wait_endpoint(cluster_id)
    return {
        "PhysicalResourceId": c["DbClusterResourceId"],
        "Data": {
            "DbClusterResourceId": c["DbClusterResourceId"],
            "Endpoint": c["Endpoint"],
            "Port": str(c.get("Port", 5432)),
        },
    }


def _ensure_log_exports(c: dict) -> None:
    """Enable any supported CloudWatch log export not already on. Idempotent —
    lets a redeploy bring an already-existing cluster up to the desired state."""
    enabled = set(c.get("EnabledCloudwatchLogsExports") or [])
    to_enable = [t for t in _LOG_EXPORTS if t not in enabled]
    if to_enable:
        rds.modify_db_cluster(
            DBClusterIdentifier=c["DBClusterIdentifier"],
            CloudwatchLogsExportConfiguration={"EnableLogTypes": to_enable},
            ApplyImmediately=True,
        )


def _current(cluster_id: str) -> dict:
    c = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
    _ensure_log_exports(c)
    return {
        "PhysicalResourceId": c["DbClusterResourceId"],
        "Data": {
            "DbClusterResourceId": c["DbClusterResourceId"],
            "Endpoint": c.get("Endpoint", ""),
            "Port": str(c.get("Port", 5432)),
        },
    }


def _on_delete(cluster_id: str) -> None:
    try:
        c = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
    except rds.exceptions.DBClusterNotFoundFault:
        return  # already gone — idempotent

    for m in c.get("DBClusterMembers", []):
        iid = m["DBInstanceIdentifier"]
        try:
            rds.delete_db_instance(DBInstanceIdentifier=iid, SkipFinalSnapshot=True)
        except rds.exceptions.InvalidDBInstanceStateFault:
            pass

    deadline = time.time() + _DELETE_TIMEOUT
    while time.time() < deadline:
        try:
            c = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
        except rds.exceptions.DBClusterNotFoundFault:
            return
        if not c.get("DBClusterMembers"):
            break
        time.sleep(_POLL_SECONDS)

    try:
        rds.delete_db_cluster(DBClusterIdentifier=cluster_id, SkipFinalSnapshot=True)
    except rds.exceptions.DBClusterNotFoundFault:
        return


def handler(event, context):
    """cr.Provider on_event contract: return a dict (no manual CFN response)."""
    rtype = event["RequestType"]
    cluster_id = event["ResourceProperties"]["ClusterId"]

    if rtype == "Create":
        return _on_create(cluster_id)
    if rtype == "Delete":
        _on_delete(cluster_id)
        return {"PhysicalResourceId": event.get("PhysicalResourceId", cluster_id)}
    # Update — express clusters aren't mutated here; return current attributes.
    return _current(cluster_id)
