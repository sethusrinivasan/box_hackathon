"""
canary.py — hourly synthetic canary for the public Reality Check web demo.

An EventBridge schedule invokes this Lambda once an hour (see the CDK
CanaryStack). It probes the live Function URL end-to-end — the landing page, the
health check, a contract review, and the report PDF — asserts the responses look
right, and emits CloudWatch metrics so the dashboard shows whether the demo is
actually working, not just "deployed".

Metrics (namespace RealityCheck, dimension endpoint where useful):
  CanaryRun       1 per run
  CanarySuccess   1 if every check passed, else 0
  CanaryFailure   1 if any check failed, else 0
  CanaryLatencyMs per-endpoint round-trip latency (Milliseconds)
  CanaryCheck     1 per endpoint check, dimension result=pass|fail

Environment:
  DEMO_URL        base URL of the web demo (set by the CDK stack)
  EMIT_METRICS    'true' so emit_metric writes EMF (set by the stack)
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "box")))

from telemetry import get_logger, log_event, emit_metric, set_request_id  # noqa: E402

_log = get_logger("reality_check.canary")

DEMO_URL = (os.environ.get("DEMO_URL") or "").rstrip("/")
_TIMEOUT = float(os.environ.get("CANARY_TIMEOUT", "20"))


def _request(method: str, path: str, body: dict | None = None):
    """Return (status, raw_bytes, latency_ms). Never raises on HTTP errors."""
    url = f"{DEMO_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
            return resp.status, raw, round((time.perf_counter() - start) * 1000, 1)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), round((time.perf_counter() - start) * 1000, 1)
    except Exception as exc:  # network/timeout
        log_event(_log, "canary.request_error", level=40, path=path, error=str(exc))
        return 0, b"", round((time.perf_counter() - start) * 1000, 1)


# Each check: (name, method, path, body, validator(status, raw) -> bool)
def _checks():
    return [
        ("healthz", "GET", "/healthz", None,
         lambda s, r: s == 200 and json.loads(r).get("ok") is True),
        ("landing", "GET", "/", None,
         lambda s, r: s == 200 and b"Reality Check" in r),
        ("review", "POST", "/api/run", {"contract": "northstar"},
         lambda s, r: s == 200 and json.loads(r)["result"]["summary"]["Risk identified"] == 3),
        ("report_pdf", "GET", "/report.pdf?contract=northstar", None,
         lambda s, r: s == 200 and r[:5] == b"%PDF-"),
    ]


def run_canary() -> dict:
    """Probe every endpoint, emit metrics, return a summary dict."""
    if not DEMO_URL:
        raise RuntimeError("DEMO_URL not configured")
    emit_metric("CanaryRun", 1, "Count")
    results = []
    all_ok = True
    for name, method, path, body, validate in _checks():
        status, raw, latency = _request(method, path, body)
        try:
            ok = bool(validate(status, raw))
        except Exception:
            ok = False
        all_ok = all_ok and ok
        emit_metric("CanaryLatencyMs", latency, "Milliseconds", endpoint=name)
        emit_metric("CanaryCheck", 1, "Count", endpoint=name, result="pass" if ok else "fail")
        log_event(_log, "canary.check", endpoint=name, status=status,
                  latency_ms=latency, ok=ok)
        results.append({"endpoint": name, "status": status, "latency_ms": latency, "ok": ok})

    emit_metric("CanarySuccess", 1 if all_ok else 0, "Count")
    emit_metric("CanaryFailure", 0 if all_ok else 1, "Count")
    summary = {"ok": all_ok, "checks": results, "demo_url": DEMO_URL}
    log_event(_log, "canary.result", ok=all_ok,
              failed=[c["endpoint"] for c in results if not c["ok"]])
    return summary


def lambda_handler(event=None, context=None):
    set_request_id(getattr(context, "aws_request_id", None))
    summary = run_canary()
    # Fail the invocation on a bad canary so Lambda Errors + the alarm also fire.
    if not summary["ok"]:
        raise RuntimeError(f"canary failed: {[c for c in summary['checks'] if not c['ok']]}")
    return summary


if __name__ == "__main__":
    # Local run: point DEMO_URL at the live demo and print the result.
    os.environ.setdefault("EMIT_METRICS", "false")
    print(json.dumps(run_canary(), indent=2))
