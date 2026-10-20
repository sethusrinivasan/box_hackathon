"""
agent/cdc/metrics.py — CloudWatch EMF metric emitter for Aurora-to-DuckDB CDC.

Emits structured logs in CloudWatch Embedded Metric Format (EMF) to stdout,
allowing CloudWatch Logs to automatically extract metrics without extra API calls.
Default namespace: RealityCheckCDC.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

_NAMESPACE = os.environ.get("CDC_METRIC_NAMESPACE", "RealityCheckCDC")


def _is_enabled() -> bool:
    return os.environ.get("EMIT_METRICS", "true").strip().lower() in ("1", "true", "yes", "on")


def emit_metric(
    name: str,
    value: float,
    unit: str = "Count",
    namespace: str | None = None,
    **dimensions: Any,
) -> None:
    """Emit a single EMF metric to stdout."""
    if not _is_enabled():
        return
    ns = namespace or _NAMESPACE
    dims = {str(k): str(v) for k, v in dimensions.items()}
    emf = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": ns,
                "Dimensions": [list(dims.keys())] if dims else [[]],
                "Metrics": [{"Name": name, "Unit": unit}],
            }],
        },
        name: value,
        **dims,
    }
    sys.stdout.write(json.dumps(emf, default=str) + "\n")
    sys.stdout.flush()


def emit_sync_metrics(
    lag_seconds: float,
    lag_bytes: int,
    events_count: int,
    duration_ms: float,
    reinit_count: int = 0,
    **dimensions: Any,
) -> None:
    """Emit a consolidated bundle of CDC sync worker metrics."""
    if not _is_enabled():
        return
    dims = {str(k): str(v) for k, v in dimensions.items()}
    emf = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": _NAMESPACE,
                "Dimensions": [list(dims.keys())] if dims else [[]],
                "Metrics": [
                    {"Name": "ReplicationLagSeconds", "Unit": "Seconds"},
                    {"Name": "ReplicationLagBytes", "Unit": "Bytes"},
                    {"Name": "EventsProcessed", "Unit": "Count"},
                    {"Name": "SyncDurationMs", "Unit": "Milliseconds"},
                    {"Name": "FullReInitCount", "Unit": "Count"},
                ],
            }],
        },
        "ReplicationLagSeconds": max(0.0, float(lag_seconds)),
        "ReplicationLagBytes": max(0, int(lag_bytes)),
        "EventsProcessed": max(0, int(events_count)),
        "SyncDurationMs": max(0.0, float(duration_ms)),
        "FullReInitCount": max(0, int(reinit_count)),
        **dims,
    }
    sys.stdout.write(json.dumps(emf, default=str) + "\n")
    sys.stdout.flush()
