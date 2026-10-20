"""
metrics.py — minimal CloudWatch EMF metric emitter for the cost Lambdas.

Mirrors box/telemetry.py's EMF shape, but standalone so the cost_lambda package
has no cross-package import. EMF lets CloudWatch Logs auto-extract metrics with
no extra API calls, so emitting is just a structured stdout line.

Namespace defaults to "RealityCheckCost" so cost-infra metrics (DB connect
latency, cold-resume retries) sit apart from the app's "RealityCheck" metrics.

We especially track Aurora Serverless v2 COLD-RESUME behavior: when the cluster
is auto-paused (min 0 ACU), the first connection can take ~15s to resume. By
recording connect latency and whether a retry/resume happened on every call, the
dashboard can show how this trends over time — and whether AWS improves resume
latency in future platform updates.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

_NAMESPACE = os.environ.get("TELEMETRY_METRIC_NAMESPACE", "RealityCheckCost")


def _enabled() -> bool:
    return os.environ.get("EMIT_METRICS", "true").strip().lower() in ("1", "true", "yes", "on")


def emit_metric(name: str, value: float, unit: str = "Count", **dimensions: Any) -> None:
    """Emit one CloudWatch EMF metric line to stdout (captured by CloudWatch
    Logs in Lambda). No-op when EMIT_METRICS is off."""
    if not _enabled():
        return
    dims = {str(k): str(v) for k, v in dimensions.items()}
    emf = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": _NAMESPACE,
                "Dimensions": [list(dims.keys())] if dims else [[]],
                "Metrics": [{"Name": name, "Unit": unit}],
            }],
        },
        name: value,
        **dims,
    }
    sys.stdout.write(json.dumps(emf, default=str) + "\n")
