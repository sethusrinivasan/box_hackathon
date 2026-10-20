"""
feature_flags.py — tiny SSM-backed feature flag for the web demo.

Design choice (frugal): AWS Systems Manager Parameter Store, Standard tier. A
single String parameter holds "on"/"off". Standard parameters and
standard-throughput GetParameter/PutParameter calls are FREE (no storage or API
charge) — the right fit for one boolean flipped from a demo page. AppConfig's
flag semantics and per-call/per-config pricing would be wasted here.

Security — a calculated, restrictive risk:
  The public web Lambda can both READ and WRITE this ONE parameter (ssm:Get/
  PutParameter scoped to a single parameter ARN — no wildcards, no other SSM
  access). That makes the demo page fully self-contained: anyone can flip the
  toggle. The blast radius is exactly one boolean in {on, off}: no data is
  exposed, no other parameter is reachable, and there is no path to privilege
  escalation. For a public demo flag that is an acceptable trade for a one-click
  toggle. (For a production flag, gate writes behind auth instead.)

Values are cached in-process briefly so a burst of requests doesn't hammer SSM.
"""
from __future__ import annotations

import os
import time

FLAG_PARAM = os.environ.get("EVIDENCE_FLAG_PARAM",
                            "/reality-check/web-demo/evidence-store")
REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_REGION_NAME", "us-east-1")
_CACHE_TTL = float(os.environ.get("FLAG_CACHE_TTL", "5"))

_cache: dict = {"value": None, "ts": 0.0}
_clients: dict = {}


def _ssm():
    if "ssm" not in _clients:
        import boto3
        _clients["ssm"] = boto3.client("ssm", region_name=REGION)
    return _clients["ssm"]


def _norm(v, default: str = "off") -> str:
    return "on" if str(v or default).strip().lower() in ("on", "1", "true", "yes") else "off"


def get_flag(default: str = "off") -> str:
    """Current flag value ("on"/"off"). Cached briefly; falls back to default on
    any error (e.g. no SSM permission locally) so the page never breaks."""
    now = time.time()
    if _cache["value"] is not None and (now - _cache["ts"]) < _CACHE_TTL:
        return _cache["value"]
    try:
        resp = _ssm().get_parameter(Name=FLAG_PARAM)
        val = _norm(resp["Parameter"]["Value"], default)
    except Exception:
        val = default
    _cache.update(value=val, ts=now)
    return val


def is_enabled() -> bool:
    return get_flag() == "on"


def set_flag(value) -> dict:
    """Flip the flag directly (scoped ssm:PutParameter on the one param). Only
    ever writes the normalized 'on'/'off', so the stored value can't be poisoned.
    Returns {ok, value} or {ok: False, error}."""
    want = _norm(value)
    try:
        _ssm().put_parameter(Name=FLAG_PARAM, Value=want, Type="String", Overwrite=True)
        _cache.update(value=want, ts=time.time())  # reflect immediately
        return {"ok": True, "value": want}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
