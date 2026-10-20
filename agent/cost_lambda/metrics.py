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


class LatencyTracker:
    """Collects database and AWS service call latencies during request execution."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.cold_resume: bool = False

    def record(self, service: str, operation: str, duration_ms: float, **extra: Any) -> None:
        self.calls.append({
            "service": service,
            "operation": operation,
            "duration_ms": round(duration_ms, 1),
            **extra,
        })

    def summary(self) -> dict[str, Any]:
        by_svc: dict[str, dict[str, Any]] = {}
        total_ms = 0.0
        for c in self.calls:
            s = c["service"]
            dur = c["duration_ms"]
            total_ms += dur
            if s not in by_svc:
                by_svc[s] = {"service": s, "calls": 0, "total_ms": 0.0, "operations": {}}
            by_svc[s]["calls"] += 1
            by_svc[s]["total_ms"] = round(by_svc[s]["total_ms"] + dur, 1)
            op = c["operation"]
            by_svc[s]["operations"][op] = by_svc[s]["operations"].get(op, 0) + 1

        services_list = []
        for s, data in sorted(by_svc.items(), key=lambda x: x[1]["total_ms"], reverse=True):
            pct = round((data["total_ms"] / total_ms * 100), 1) if total_ms > 0 else 0.0
            services_list.append({
                "service": s,
                "calls": data["calls"],
                "total_ms": data["total_ms"],
                "percent": pct,
                "operations": data["operations"],
            })

        status = ("Aurora Serverless v2 (Cold Resume)" if self.cold_resume
                  else "Aurora Serverless v2 (Warm)")
        return {
            "total_ms": round(total_ms, 1),
            "total_calls": len(self.calls),
            "cold_resume": self.cold_resume,
            "database_status": status,
            "services": services_list,
        }
