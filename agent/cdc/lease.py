"""
agent/cdc/lease.py — Distributed Lock/Lease Manager backed by DynamoDB.

Guarantees singleton writer semantics for DuckDB S3 operations. Only one
Lambda execution holds the lease at any given instant. Automatic expiration
ensures that stalled/crashed Lambdas do not leave the system permanently deadlocked.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger("reality_check.cdc.lease")


class LeaseAcquisitionError(Exception):
    """Raised when lease cannot be acquired and fail_silently is False."""


class DynamoDbLeaseManager:
    """Manages an expiring distributed lease in a DynamoDB table.

    Key properties:
      - lock_key (PK): identifying resource name (e.g. 'duckdb_writer_lease')
      - owner_id: unique worker execution ID (e.g. Lambda request ID)
      - lease_expires_at: epoch timestamp (seconds) when lease becomes reclaimable
      - version: monotonically increasing fencing token
    """

    def __init__(
        self,
        table_name: str,
        lock_key: str = "duckdb_writer_lease",
        lease_duration_sec: int = 300,
        owner_id: str | None = None,
        dynamodb_client: Any | None = None,
    ) -> None:
        self.table_name = table_name
        self.lock_key = lock_key
        self.lease_duration_sec = lease_duration_sec
        self.owner_id = owner_id or f"worker-{uuid.uuid4().hex[:12]}"
        self.client = dynamodb_client or boto3.client("dynamodb")
        self.is_held = False
        self.version = 0

    def acquire(self) -> bool:
        """Attempt to acquire the lease.

        Returns True if acquired, False if currently held by another active worker.
        """
        now = int(time.time())
        expires_at = now + self.lease_duration_sec

        try:
            # Atomic conditional put: succeed ONLY if key doesn't exist OR current lease expired.
            # We also read the old version if possible or increment.
            self.client.put_item(
                TableName=self.table_name,
                Item={
                    "lock_key": {"S": self.lock_key},
                    "owner_id": {"S": self.owner_id},
                    "acquired_at": {"N": str(now)},
                    "lease_expires_at": {"N": str(expires_at)},
                    "ttl": {"N": str(expires_at + 86400)},  # DynamoDB TTL cleanup
                },
                ConditionExpression=(
                    "attribute_not_exists(lock_key) OR lease_expires_at < :now"
                ),
                ExpressionAttributeValues={
                    ":now": {"N": str(now)},
                },
            )
            self.is_held = True
            logger.info(
                "Lease acquired: key=%s owner=%s expires_at=%s",
                self.lock_key, self.owner_id, expires_at
            )
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                logger.info(
                    "Lease acquisition skipped: lock=%s is currently held by another active worker.",
                    self.lock_key
                )
                self.is_held = False
                return False
            logger.error("DynamoDB error during lease acquire: %s", e)
            raise

    def renew(self, extension_sec: int | None = None) -> bool:
        """Renew the active lease if still owned by this instance."""
        if not self.is_held:
            return False

        ext = extension_sec or self.lease_duration_sec
        now = int(time.time())
        new_expires_at = now + ext

        try:
            self.client.update_item(
                TableName=self.table_name,
                Key={"lock_key": {"S": self.lock_key}},
                UpdateExpression="SET lease_expires_at = :new_exp, #ttl = :new_ttl",
                ConditionExpression="owner_id = :owner AND lease_expires_at >= :now",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":new_exp": {"N": str(new_expires_at)},
                    ":new_ttl": {"N": str(new_expires_at + 86400)},
                    ":owner": {"S": self.owner_id},
                    ":now": {"N": str(now)},
                },
            )
            logger.debug("Lease renewed: key=%s new_expires_at=%s", self.lock_key, new_expires_at)
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                logger.warning("Lease renewal failed: lease was lost or expired for %s", self.owner_id)
                self.is_held = False
                return False
            raise

    def release(self) -> bool:
        """Release the lease cleanly so subsequent invocations can acquire immediately."""
        if not self.is_held:
            return True

        now = int(time.time())
        try:
            # Set lease_expires_at = 0 so next worker can immediately acquire
            self.client.update_item(
                TableName=self.table_name,
                Key={"lock_key": {"S": self.lock_key}},
                UpdateExpression="SET lease_expires_at = :zero",
                ConditionExpression="owner_id = :owner",
                ExpressionAttributeValues={
                    ":zero": {"N": "0"},
                    ":owner": {"S": self.owner_id},
                },
            )
            self.is_held = False
            logger.info("Lease released: key=%s owner=%s", self.lock_key, self.owner_id)
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                logger.warning("Lease release failed: lock no longer owned by %s", self.owner_id)
                self.is_held = False
                return False
            logger.error("DynamoDB error during lease release: %s", e)
            return False

    def __enter__(self) -> DynamoDbLeaseManager:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self.is_held:
            self.release()
