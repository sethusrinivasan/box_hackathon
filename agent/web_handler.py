"""
web_handler.py — public web portal for Reality Check contract claim reviews.

Served by an AWS Lambda Function URL. Renders a self-contained HTML page and a JSON
API supporting contract reviews, custom PDF uploads, in-browser PDF viewing,
and PDF risk report downloads. Operates in AWS-native mode backed by Amazon S3
and Amazon Bedrock (frugal Amazon Nova Lite), falling back to offline mode when
cloud resources are unconfigured.

Routes (Lambda Function URL, payload format 2.0):
  GET  /                -> Interactive web portal (HTML)
  POST /api/run         -> Run claim review; returns JSON verdict and claim list
  POST /api/upload      -> Upload contract PDF (stored in S3 or local scratch)
  GET  /api/contracts   -> List all available contracts
  GET  /contract.pdf    -> Stream raw contract PDF
  GET  /report.pdf      -> Stream generated risk-report PDF
  GET  /healthz         -> Liveness probe
  GET  /api/flag        -> Feature flag read
  POST /api/flag        -> Feature flag update
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time

# ---------------------------------------------------------------------------
# Disable Box credentials in web context; use S3Docs or LocalDocs.
# ---------------------------------------------------------------------------
for _k in ("BOX_CLIENT_ID", "BOX_CLIENT_SECRET", "BOX_ENTERPRISE_ID",
           "BOX_CONTRACTS_FOLDER_ID", "BOX_EVIDENCE_FOLDER_ID", "BOX_REPORTS_FOLDER_ID"):
    os.environ[_k] = ""
os.environ.pop("BOX_SECRET_ARN", None)
os.environ.setdefault("LOG_LEVEL", "INFO")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
for _p in (HERE, os.path.join(ROOT, "box")):
    sys.path.insert(0, _p)

import local_agent as la
import feature_flags as ff
from cost_estimate import estimate_cost
from telemetry import (
    get_logger, log_event, emit_metric, set_request_id, capture_aws_calls,
)

# Frugal default model on Amazon Bedrock ($0.06/$0.24 per 1M tokens)
_LIVE_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.amazon.nova-lite-v1:0")

_log = get_logger("reality_check.web")

# Reports/comments dir: /tmp in Lambda (read-only /var/task otherwise). LocalDocs
# writes under ROOT/reports; we point ROOT's reports at /tmp via an override.
# /tmp is the only writable dir in Lambda; env-overridable.
_REPORTS_DIR = os.environ.get("REALITY_CHECK_REPORTS_DIR", "/tmp/rc-reports")  # nosec B108


# ---------------------------------------------------------------------------
# Canned Bedrock client — deterministic review with no AWS call.
# ---------------------------------------------------------------------------
# The claims below are the verified review of the Northstar archive-migration
# contract against the B1/B2/B3 evidence (same fixtures the live agent reads).
# Serving a canned result keeps the public demo free, instant, and offline while
# exercising the real review_contract pipeline (classify -> report -> comments).
_CANNED_CLAIMS = [
    {"location": "Section 1", "claim": "The archive will support encryption at rest in the agreed configuration.",
     "result": "No risk", "risk_category": "None", "evidence_ref": "B1",
     "evidence_passage": "Encryption at rest: supported",
     "explanation": "B1 confirms encryption at rest for the Archive tier in US-West (Oregon)."},
    {"location": "Section 2", "claim": "The archive will include the proposed Smart Archive Search feature at launch.",
     "result": "Risk identified", "risk_category": "Feature availability", "evidence_ref": "B2",
     "evidence_passage": "Exploratory",
     "explanation": "Roadmap lists Smart Archive Search as Exploratory with no release date; it cannot ship at launch."},
    {"location": "Section 3", "claim": "The archive migration will be completed within 30 days of signature.",
     "result": "Risk identified", "risk_category": "Delivery deadline", "evidence_ref": "B3",
     "evidence_passage": "58-72 days",
     "explanation": "Comparable 400-800 TB migrations took 58-72 days; the contract promises 30."},
    {"location": "Section 4", "claim": "The archive will sustain read throughput of 10 GB/s in the agreed configuration.",
     "result": "Risk identified", "risk_category": "Performance commitment", "evidence_ref": "B1",
     "evidence_passage": "5 GB/s",
     "explanation": "Benchmark validates 5 GB/s sustained; 10 GB/s has not been tested."},
    {"location": "Section 5", "claim": "The archive will support direct import from Northstar's LegacyVault system.",
     "result": "No source found", "risk_category": "None", "evidence_ref": "", "evidence_passage": "",
     "explanation": "No Box document mentions LegacyVault direct import; confirm with the product owner."},
]


class _CannedBedrock:
    """Stands in for boto3 bedrock-runtime: returns the canned review as a
    forced submit_review tool call, the exact shape local_agent expects."""

    def converse(self, **_kw):
        return {"output": {"message": {"content": [
            {"toolUse": {"toolUseId": "canned", "name": "submit_review",
                         "input": {"claims": _CANNED_CLAIMS}}}
        ]}}}


class _TmpDocs(la.LocalDocs):
    """LocalDocs, but reports/comments/uploads go to a writable dir (/tmp in Lambda)."""

    def __init__(self):
        super().__init__()
        self.reports = _REPORTS_DIR
        self.custom_contracts_dir = os.path.join(_REPORTS_DIR, "contracts")
        os.makedirs(self.custom_contracts_dir, exist_ok=True)

    def contracts(self) -> List[Dict[str, str]]:
        base_list = super().contracts()
        if os.path.exists(self.custom_contracts_dir):
            for n in sorted(os.listdir(self.custom_contracts_dir)):
                if n.endswith(".pdf"):
                    p = os.path.join(self.custom_contracts_dir, n)
                    base_list.append({"id": p, "name": n, "link": f"/contract.pdf?contract={n.rsplit('.', 1)[0].lower()}"})
        return base_list

    def upload_contract(self, name: str, data: bytes) -> Dict[str, str]:
        path = os.path.join(self.custom_contracts_dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return {"id": path, "name": name, "link": f"/contract.pdf?contract={name.rsplit('.', 1)[0].lower()}"}


def _get_active_docs():
    s3_bucket = os.environ.get("REALITY_CHECK_S3_BUCKET")
    if s3_bucket:
        try:
            return la.S3Docs(bucket=s3_bucket)
        except Exception as exc:
            _log.warning(f"Failed to initialize S3Docs: {exc}; using _TmpDocs")
    return _TmpDocs()


# Contracts the demo exposes (dynamically expandable with user uploads).
_CONTRACTS = {
    "northstar": "Northstar-Archive-Migration-Agreement",
    "northstar-v2": "Northstar-Archive-Migration-Agreement-v2",
}
_CUSTOM_CONTRACTS: dict[str, str] = {}
_LAST_REVIEWS: dict[str, list] = {}


def _resolve_contract_name(contract_key: str, docs=None, fallback: bool = False) -> str | None:
    """Resolve a contract query key to its actual file or display name."""
    if contract_key in _CUSTOM_CONTRACTS:
        return _CUSTOM_CONTRACTS[contract_key]
    if contract_key in _CONTRACTS:
        return _CONTRACTS[contract_key]
    d = docs or _get_active_docs()
    for c in d.contracts():
        stem = c["name"].rsplit(".", 1)[0]
        if stem.lower() == contract_key.lower() or c["name"].lower() == contract_key.lower():
            return c["name"]
    return _CONTRACTS["northstar"] if fallback else None


def _estimate_live_cost(contract_key: str, out: dict) -> dict:
    """Estimate what a real Bedrock run of this review would cost."""
    claims = out.get("claims", []) or []
    docs = _get_active_docs()
    parts = [getattr(la, "REVIEW_SYSTEM", "")]
    name = _resolve_contract_name(contract_key, docs=docs, fallback=True)
    contract = la.match_contract(docs.contracts(), name)
    if contract:
        parts.append(la.pdf_text(docs.read(contract["id"])))
    for e in docs.evidence():
        parts.append(la.pdf_text(docs.read(e["id"])))
    input_text = "\n\n".join(p for p in parts if p)
    out_tokens = max(400, len(claims) * 180)
    est = estimate_cost(_LIVE_MODEL_ID, input_text=input_text, output_tokens=out_tokens)
    return est.as_dict()


def _run_review(contract_key: str, *, live: bool = False) -> dict:
    with capture_aws_calls() as tracker:
        docs = _get_active_docs()
        name = _resolve_contract_name(contract_key, docs=docs, fallback=True)
        retrieval_on = ff.is_enabled()
        os.environ["EVIDENCE_STORE"] = "on" if retrieval_on else "off"

        client = None
        use_live_bedrock = live or os.environ.get("BEDROCK_ENABLED") == "true"
        if use_live_bedrock:
            try:
                client = la.bedrock()
            except Exception as exc:
                _log.warning(f"Bedrock invocation failed ({exc}); falling back to deterministic review")
                client = _CannedBedrock()
        else:
            client = _CannedBedrock()

        out = la.review_contract(docs, name, client)
        _LAST_REVIEWS[contract_key] = out.get("claims", [])
        s = out.get("summary", {})
        emit_metric("WebDemoReview", 1, "Count", contract=contract_key)
        emit_metric("WebDemoRisksFound", s.get("Risk identified", 0), "Count")
        emit_metric("WebDemoEvidenceMode", 1, "Count",
                    mode="pgvector" if retrieval_on else "full-corpus")
        is_live = client != _CannedBedrock and not isinstance(client, _CannedBedrock)
        result = {
            "contract": out.get("contract"),
            "contract_key": contract_key,
            "summary": s,
            "claims": out.get("claims", []),
            "sources": out.get("sources", []),
            "report_name": out.get("report_name"),
            "comments_posted": out.get("comments_posted_on_contract"),
            "mode": "live (bedrock)" if is_live else "mock",
            "evidence_retrieval": "pgvector (semantic top-k)" if retrieval_on
                                   else "full-corpus",
            "aws_breakdown": tracker.summary(),
        }
        if live or not is_live:
            result["live_cost_estimate"] = _estimate_live_cost(contract_key, out)
        return result


# ---------------------------------------------------------------------------
# HTTP layer (Lambda Function URL, payload format 2.0)
# ---------------------------------------------------------------------------
def _page_html() -> str:
    """Render the demo page, injecting the 'Cloud cost' link when the cost web
    view URL is configured (env COST_WEB_URL set by deploy). If unset, the link
    is omitted so the page still works standalone."""
    cost_url = os.environ.get("COST_WEB_URL", "").strip()
    link = (f'<a href="{cost_url}">Cloud cost &rarr;</a> '
            "— holistic daily AWS spend for this demo account."
            if cost_url else "")
    return PAGE_HTML.replace("{{COST_LINK}}", link)


def _json(status, body):
    return {"statusCode": status,
            "headers": {"content-type": "application/json", "cache-control": "no-store"},
            "body": json.dumps(body, default=str)}


def _html(status, body):
    return {"statusCode": status,
            "headers": {"content-type": "text/html; charset=utf-8", "cache-control": "no-store"},
            "body": body}


def _method_path(event):
    rc = event.get("requestContext", {}).get("http", {})
    return (rc.get("method") or event.get("httpMethod") or "GET").upper(), \
           (rc.get("path") or event.get("rawPath") or "/")


def _parse_body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}


_NOISE = ("/favicon.ico", "/robots.txt", "/apple-touch-icon.png",
          "/apple-touch-icon-precomposed.png")


def lambda_handler(event, context=None):
    method, path = _method_path(event)
    rid = set_request_id(getattr(context, "aws_request_id", None))

    if path in _NOISE:
        return {"statusCode": 404, "headers": {"cache-control": "no-store"}, "body": ""}

    src_ip = event.get("requestContext", {}).get("http", {}).get("sourceIp")
    log_event(_log, "web.request", method=method, path=path, source_ip=src_ip)

    start = time.perf_counter()
    status = 500
    contract = None
    try:
        if path in ("/", "/index.html") and method in ("GET", "HEAD"):
            status = 200
            if method == "HEAD":
                return {"statusCode": 200,
                        "headers": {"content-type": "text/html; charset=utf-8", "cache-control": "no-store"},
                        "body": ""}
            return _html(200, _page_html())
        if path == "/healthz" and method in ("GET", "HEAD"):
            status = 200
            if method == "HEAD":
                return {"statusCode": 200,
                        "headers": {"content-type": "application/json", "cache-control": "no-store"},
                        "body": ""}
            return _json(200, {"ok": True, "mode": "offline",
                               "contracts": list(_CONTRACTS)})
        if path == "/api/flag" and method == "GET":
            # Public READ of the feature flag (reader role: ssm:GetParameter on
            # the one param only). Used by the page to render the toggle state.
            status = 200
            return _json(200, {"evidence_store": ff.get_flag()})
        if path == "/api/flag" and method == "POST":
            # Direct FLIP from the page. The reader role holds ssm:Put/GetParameter
            # scoped to ONLY this one parameter ARN, and set_flag writes only the
            # normalized {on, off} — so the blast radius is a single demo boolean
            # with no data exposure and no escalation. A calculated, restrictive
            # trade for a one-click toggle on a public demo.
            body = _parse_body(event)
            out = ff.set_flag(body.get("value", "off"))
            status = 200 if out.get("ok") else 500
            emit_metric("WebDemoFlagFlip", 1, "Count", state=out.get("value", "?"))
            return _json(status, out)
        if path == "/api/run" and method == "POST":
            body = _parse_body(event)
            contract = (body.get("contract") or "northstar").strip()
            live = bool(body.get("live"))
            status = 200
            return _json(200, {"contract_key": contract,
                               "result": _run_review(contract, live=live)})
        if path == "/api/estimate" and method in ("GET", "POST"):
            # Standalone cost heads-up: run the mock to size inputs, return only
            # the estimated real-run cost (no review payload).
            body = _parse_body(event) if method == "POST" else {}
            qs = event.get("queryStringParameters") or {}
            contract = (body.get("contract") or qs.get("contract") or "northstar").strip()
            name = _CONTRACTS.get(contract, _CONTRACTS["northstar"])
            out = la.review_contract(_TmpDocs(), name, _CannedBedrock())
            status = 200
            return _json(200, {"contract_key": contract, "mode": "mock",
                               "live_cost_estimate": _estimate_live_cost(contract, out)})
        if path == "/api/upload" and method == "POST":
            body = _parse_body(event)
            filename = (body.get("filename") or body.get("name") or "uploaded-contract.pdf").strip()
            import re
            safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(filename))
            if not safe_name.lower().endswith(".pdf"):
                safe_name += ".pdf"
            raw_b64 = body.get("data") or body.get("content") or ""
            if not raw_b64:
                return _json(400, {"error": "missing base64 'data' in request body"})
            try:
                pdf_bytes = base64.b64decode(raw_b64)
            except Exception as e:
                return _json(400, {"error": f"invalid base64 encoding: {e}"})

            docs = _get_active_docs()
            info = docs.upload_contract(safe_name, pdf_bytes)
            contract_key = safe_name.rsplit(".", 1)[0].lower()
            _CUSTOM_CONTRACTS[contract_key] = safe_name
            status = 200
            emit_metric("WebDemoContractUpload", 1, "Count", contract=contract_key)
            return _json(200, {
                "ok": True,
                "contract_key": contract_key,
                "name": safe_name,
                "size_bytes": len(pdf_bytes),
                "link": info.get("link", f"/contract.pdf?contract={contract_key}"),
            })
        if path == "/api/contracts" and method == "GET":
            status = 200
            docs = _get_active_docs()
            seen = set()
            items = []
            for k, v in _CONTRACTS.items():
                seen.add(k)
                items.append({"key": k, "name": v})
            for k, v in _CUSTOM_CONTRACTS.items():
                if k not in seen:
                    seen.add(k)
                    items.append({"key": k, "name": v})
            for c in docs.contracts():
                stem = c["name"].rsplit(".", 1)[0]
                ckey = stem.lower()
                if ckey not in seen:
                    seen.add(ckey)
                    items.append({"key": ckey, "name": stem})
            return _json(200, {"contracts": items})
        if path == "/contract.pdf" and method in ("GET", "HEAD"):
            qs = event.get("queryStringParameters") or {}
            contract = (qs.get("contract") or "northstar").strip()
            name = _resolve_contract_name(contract)
            if not name:
                return _json(404, {"error": f"no contract '{contract}'"})
            if method == "HEAD":
                return {"statusCode": 200,
                        "headers": {"content-type": "application/pdf",
                                    "content-disposition": f'inline; filename="{contract}.pdf"',
                                    "cache-control": "no-store"},
                        "body": ""}
            resp = _contract_pdf(contract)
            status = resp["statusCode"]
            return resp
        if path == "/report.pdf" and method in ("GET", "HEAD"):
            qs = event.get("queryStringParameters") or {}
            contract = (qs.get("contract") or "northstar").strip()
            name = _resolve_contract_name(contract)
            if not name:
                return _json(404, {"error": f"no contract '{contract}'"})
            if method == "HEAD":
                return {"statusCode": 200,
                        "headers": {"content-type": "application/pdf",
                                    "content-disposition": f'inline; filename="RealityCheck-{contract}.pdf"',
                                    "cache-control": "no-store"},
                        "body": ""}
            resp = _report_pdf(contract)
            status = resp["statusCode"]
            return resp
        status = 404
        return _json(404, {"error": f"not found: {method} {path}"})
    except Exception as exc:  # last-resort guard so the page never 500s silently
        status = 500
        log_event(_log, "web.error", level=logging.ERROR, path=path,
                  contract=contract, error=str(exc), error_type=type(exc).__name__)
        emit_metric("WebDemoError", 1, "Count")
        return _json(500, {"error": "internal error", "detail": str(exc)})
    finally:
        dur = round((time.perf_counter() - start) * 1000, 1)
        log_event(_log, "web.response", method=method, path=path,
                  status=status, duration_ms=dur, contract=contract)
        emit_metric("WebResponse", 1, "Count", status=str(status))


def _report_pdf(contract_key: str) -> dict:
    """Build the risk-report PDF for a contract and return it inline (base64)."""
    docs = _get_active_docs()
    name = _resolve_contract_name(contract_key, docs=docs)
    if not name:
        return _json(404, {"error": f"no contract '{contract_key}'"})
    contract = la.match_contract(docs.contracts(), name)
    if not contract:
        return _json(404, {"error": f"no contract '{contract_key}'"})
    sources = []
    for i, e in enumerate(docs.evidence(), 1):
        text = la.pdf_text(docs.read(e["id"]))
        title = text.strip().splitlines()[0] if text.strip() else e["name"]
        sources.append({**e, "ref": f"B{i}", "text": text, "title": title,
                        "last_updated": la.last_updated(text)})

    raw_claims = _LAST_REVIEWS.get(contract_key)
    if raw_claims is None:
        if contract_key not in _CONTRACTS:
            rev = _run_review(contract_key)
            raw_claims = rev.get("claims", [])
        else:
            raw_claims = _CANNED_CLAIMS

    claims = la.by_severity(la.classify(raw_claims))
    counts = la.summarize(claims)
    pdf = la.build_report(contract, claims, counts, sources)
    emit_metric("WebDemoPdf", 1, "Count", contract=contract_key)
    return {"statusCode": 200,
            "headers": {"content-type": "application/pdf",
                        "content-disposition": f'inline; filename="RealityCheck-{contract_key}.pdf"',
                        "cache-control": "no-store"},
            "isBase64Encoded": True,
            "body": base64.b64encode(pdf).decode("ascii")}


def _contract_pdf(contract_key: str) -> dict:
    """Return raw contract PDF bytes inline for viewing or download."""
    docs = _get_active_docs()
    name = _resolve_contract_name(contract_key, docs=docs)
    if not name:
        return _json(404, {"error": f"no contract '{contract_key}'"})
    contract = la.match_contract(docs.contracts(), name)
    if not contract:
        return _json(404, {"error": f"no contract '{contract_key}'"})
    data = docs.read(contract["id"])
    return {"statusCode": 200,
            "headers": {"content-type": "application/pdf",
                        "content-disposition": f'inline; filename="{os.path.basename(contract["id"])}"',
                        "cache-control": "no-store"},
            "isBase64Encoded": True,
            "body": base64.b64encode(data).decode("ascii")}


# ---------------------------------------------------------------------------
# The single page. Self-contained: no external JS/CSS/CDN.
# ---------------------------------------------------------------------------
PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reality Check — live demo</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --line:#30363d; --fg:#e6edf3;
          --muted:#9da7b3; --red:#f85149; --amber:#d29922; --green:#3fb950; --accent:#58a6ff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:15px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }
  header, main { max-width:900px; margin:0 auto; padding:0 20px; }
  header { padding-top:28px; } h1 { margin:0 0 4px; font-size:24px; }
  .tag { color:var(--muted); margin:0 0 6px; }
  .row { display:flex; flex-wrap:wrap; gap:10px; margin:16px 0; }
  button { background:var(--card); color:var(--fg); border:1px solid var(--line);
           border-radius:8px; padding:10px 14px; cursor:pointer; font-size:14px; }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); color:#0d1117; border-color:var(--accent); font-weight:600; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px 18px; margin:14px 0; }
  .muted { color:var(--muted); } a { color:var(--accent); }
  .pill { display:inline-block; padding:2px 9px; border-radius:999px; font-size:12px; font-weight:600; }
  .High { background:rgba(248,81,73,.15); color:var(--red); }
  .Medium { background:rgba(210,153,34,.15); color:var(--amber); }
  .None { background:rgba(63,185,80,.15); color:var(--green); }
  .claim { border-left:3px solid var(--line); padding:6px 0 6px 12px; margin:10px 0; }
  .foot { color:var(--muted); font-size:12px; margin:30px 0 60px; }
  code { background:#0b0f14; padding:1px 5px; border-radius:4px; }
  .flagrow { display:flex; align-items:center; justify-content:space-between; gap:16px; }
  .switch { position:relative; display:inline-block; width:46px; height:26px; flex:0 0 auto; }
  .switch input { opacity:0; width:0; height:0; }
  .slider { position:absolute; inset:0; cursor:pointer; background:#30363d;
            border-radius:999px; transition:.2s; }
  .slider:before { content:""; position:absolute; height:20px; width:20px; left:3px; top:3px;
            background:#e6edf3; border-radius:50%; transition:.2s; }
  input:checked + .slider { background:var(--green); }
  input:checked + .slider:before { transform:translateX(20px); }
  input:disabled + .slider { opacity:.5; cursor:wait; }
</style>
</head>
<body>
<header>
  <h1>Reality Check <span class="muted">— live demo</span></h1>
  <p class="tag">Check a draft sales contract's claims against the company's evidence in Box, before you commit.</p>
  <p class="muted">The agent extracts every feature, delivery, and performance claim, checks each against
  approved docs / roadmap / delivery reviews, and flags what can't be supported — then comments the
  result on the contract itself. Everything here runs <strong>offline</strong> on synthetic data — no login.</p>
  <p class="tag">{{COST_LINK}}</p>
</header>
<main>
  <div class="card" id="flagCard">
    <div class="flagrow">
      <div>
        <strong>Evidence retrieval</strong>
        <span id="flagState" class="pill None" style="margin-left:6px">…</span>
        <div class="muted" style="font-size:13px;margin-top:4px" id="flagDesc">
          Loading feature flag…
        </div>
      </div>
      <label class="switch" title="Toggle pgvector semantic retrieval">
        <input type="checkbox" id="flagToggle" onchange="flipFlag(this.checked)">
        <span class="slider"></span>
      </label>
    </div>
    <div class="muted" style="font-size:12px;margin-top:10px">
      <strong>Full-corpus</strong>: every evidence doc goes into the prompt (fine for a few docs).
      <strong>pgvector</strong>: embed the corpus once, retrieve only the top-k relevant passages per
      review — bounded tokens, scales past the context window. Measured on a 100-doc corpus:
      <strong>~95% fewer evidence tokens</strong>. Flag lives in SSM Parameter Store (free tier);
      the demo is offline so this drives the <em>reported</em> mode. See the
      <a href="https://github.com/sethusrinivasan/box_hackathon/blob/fix/gross-usage-cost/docs/DESIGN_pgvector_evidence_retrieval.md">design doc</a>.
    </div>
  </div>
  <div class="card" id="uploadCard">
    <div style="font-weight:600;margin-bottom:6px">Upload Custom Sales Contract (PDF)</div>
    <div class="muted" style="font-size:13px;margin-bottom:12px">
      Drag &amp; drop or select any draft customer agreement to store in AWS S3 and run an immediate risk review against ground-truth evidence.
    </div>
    <div id="dropZone" style="border:2px dashed var(--line); border-radius:8px; padding:18px; text-align:center; cursor:pointer; background:rgba(255,255,255,0.02); transition:border-color .2s" onclick="document.getElementById('fileInput').click()">
      <input type="file" id="fileInput" accept=".pdf" style="display:none" onchange="handleFileSelect(event)">
      <div id="dropText">📄 Drop a draft contract PDF here or <span style="color:var(--accent);text-decoration:underline">browse files</span></div>
    </div>
    <div id="uploadStatus" class="muted" style="font-size:12px;margin-top:8px"></div>
  </div>
  <div class="row">
    <button class="primary" onclick="run('northstar')">Review the Northstar archive contract</button>
    <button onclick="run('northstar-v2')">Review the v2 draft</button>
  </div>
  <div id="out"></div>
  <p class="foot">Synthetic data; fictional customer and evidence. Served by an AWS Lambda function URL
  running the offline review pipeline. Source:
  <a href="https://github.com/sethusrinivasan/box_hackathon">github.com/sethusrinivasan/box_hackathon</a>.</p>
</main>
<script>
const out = document.getElementById('out');
function esc(s){ return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function pill(sev){ return `<span class="pill ${esc(sev)}">${sev==='None'?'OK':esc(sev)}</span>`; }

// --- Feature flag (SSM-backed) --------------------------------------------
const flagToggle = document.getElementById('flagToggle');
const flagState  = document.getElementById('flagState');
const flagDesc   = document.getElementById('flagDesc');
function paintFlag(on){
  flagToggle.checked = on;
  flagState.textContent = on ? 'pgvector' : 'full-corpus';
  flagState.className = 'pill ' + (on ? 'None' : 'Medium');
  flagDesc.innerHTML = on
    ? 'Semantic top-k retrieval is <strong>ON</strong> — reviews pull only the most relevant evidence passages.'
    : 'Full-corpus is <strong>ON</strong> — reviews send every evidence doc to the model.';
}
async function loadFlag(){
  try{ const r = await fetch('/api/flag'); const d = await r.json();
       paintFlag(d.evidence_store === 'on'); }
  catch(e){ flagDesc.textContent = 'Feature flag unavailable.'; }
}
async function flipFlag(on){
  flagToggle.disabled = true;
  flagDesc.textContent = 'Updating flag…';
  try{
    const r = await fetch('/api/flag', {method:'POST', headers:{'content-type':'application/json'},
                                        body: JSON.stringify({value: on ? 'on' : 'off'})});
    const d = await r.json();
    if(!r.ok || !d.ok){ flagDesc.textContent = 'Flip failed: ' + esc(d.error || r.status); paintFlag(!on); }
    else { paintFlag(d.value === 'on'); }
  }catch(e){ flagDesc.textContent = 'Flip failed: ' + esc(e.message); paintFlag(!on); }
  finally{ flagToggle.disabled = false; }
}
loadFlag();

async function loadContracts() {
  try {
    const r = await fetch('/api/contracts');
    const d = await r.json();
    const row = document.querySelector('.row');
    if (!row || !d.contracts || !d.contracts.length) return;
    row.innerHTML = '';
    d.contracts.forEach((c, idx) => {
      const btn = document.createElement('button');
      btn.id = 'btn-' + c.key;
      btn.className = idx === 0 ? 'primary' : '';
      btn.textContent = `Review ${c.name}`;
      btn.onclick = () => run(c.key);
      row.appendChild(btn);
    });
  } catch(e) {}
}
loadContracts();

const dropZone = document.getElementById('dropZone');
if (dropZone) {
  dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.style.borderColor = 'var(--accent)'; });
  dropZone.addEventListener('dragleave', () => { dropZone.style.borderColor = 'var(--line)'; });
  dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.style.borderColor = 'var(--line)';
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      uploadContract(e.dataTransfer.files[0]);
    }
  });
}

function handleFileSelect(e) {
  if (e.target.files && e.target.files[0]) {
    uploadContract(e.target.files[0]);
  }
}

async function uploadContract(file) {
  if (!file.name.toLowerCase().endsWith('.pdf')) {
    alert('Please select a PDF document.');
    return;
  }
  const status = document.getElementById('uploadStatus');
  status.innerHTML = `Uploading <strong>${esc(file.name)}</strong> (${(file.size/1024).toFixed(1)} KB)…`;
  const reader = new FileReader();
  reader.onload = async function() {
    const b64 = reader.result.split(',')[1];
    try {
      const res = await fetch('/api/upload', {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({filename: file.name, data: b64})
      });
      const d = await res.json();
      if (d.ok) {
        status.innerHTML = `✅ Stored in AWS S3: <strong>${esc(d.name)}</strong>. Running risk review…`;
        let btn = document.getElementById('btn-' + d.contract_key);
        if (!btn) {
          btn = document.createElement('button');
          btn.id = 'btn-' + d.contract_key;
          btn.className = 'primary';
          btn.textContent = `Review ${d.name}`;
          btn.onclick = () => run(d.contract_key);
          const row = document.querySelector('.row');
          if (row) row.prepend(btn);
        }
        run(d.contract_key);
      } else {
        status.textContent = 'Upload failed: ' + (d.error || 'unknown');
      }
    } catch(err) {
      status.textContent = 'Upload error: ' + err.message;
    }
  };
  reader.readAsDataURL(file);
}

async function run(contract){
  out.innerHTML = '<div class="card muted">Reviewing…</div>';
  try{
    const resp = await fetch('/api/run', {method:'POST', headers:{'content-type':'application/json'},
                                          body: JSON.stringify({contract})});
    const data = await resp.json();
    if(!resp.ok){ out.innerHTML = `<div class="card">Error: ${esc(data.error)}</div>`; return; }
    out.innerHTML = render(contract, data.result);
  }catch(e){ out.innerHTML = `<div class="card">Request failed: ${esc(e.message)}</div>`; }
}

function render(key, r){
  const s = r.summary || {};
  let h = `<div class="card"><h3 style="margin-top:0">${esc(r.contract)}</h3>`;
  h += `<p><strong>${esc(s['Risk identified'])}</strong> of ${esc((r.claims||[]).length)} claims need attention before you commit `;
  h += `<span class="muted">· ${esc(s['No risk'])} no risk · ${esc(s['No source found'])} no source found</span></p>`;
  h += `<p class="muted">Severity: ${esc(s.severity_High)} High · ${esc(s.severity_Medium)} Medium · ${esc(s.severity_None)} None</p>`;
  if(r.evidence_retrieval) h += `<p class="muted">Evidence retrieval: <strong>${esc(r.evidence_retrieval)}</strong></p>`;
  (r.claims||[]).forEach(c=>{
    h += `<div class="claim">${pill(c.severity)} <strong>${esc(c.result)}</strong>`;
    if(c.risk_category && c.risk_category!=='None') h += ` <span class="muted">· ${esc(c.risk_category)}</span>`;
    h += ` <span class="muted">· ${esc(c.location)}</span>`;
    h += `<br>"${esc(c.claim)}"<br><span class="muted">${esc(c.explanation)}</span>`;
    if(c.evidence_ref) h += ` <span class="muted">[source ${esc(c.evidence_ref)}]</span>`;
    h += `</div>`;
  });
  h += `<p class="muted" style="margin-top:12px">Comments and risk findings recorded (${esc(r.comments_posted)} comments).</p>`;
  h += `<p style="margin-top:14px">` +
       `<a href="/report.pdf?contract=${esc(key)}" target="_blank" style="font-weight:600">Open Risk Report PDF →</a>` +
       `<a href="/contract.pdf?contract=${esc(key)}" target="_blank" style="margin-left:16px">View Contract PDF →</a>` +
       `</p>`;
  const b = r.aws_breakdown;
  if (b && b.total_calls > 0) {
    h += `<div style="margin-top:16px; padding:12px 14px; background:#0d1117; border:1px solid var(--line); border-radius:8px">`;
    h += `<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px">`;
    h += `<strong style="font-size:13px">⚡ AWS Service Latency &amp; Call Breakdown</strong>`;
    h += `<span class="muted" style="font-size:12px">${esc(b.total_calls)} calls · ${esc(b.total_aws_ms)} ms total</span>`;
    h += `</div>`;
    h += `<table style="width:100%; font-size:12px; border-collapse:collapse; margin-top:6px">`;
    h += `<thead><tr style="text-align:left; color:var(--muted); border-bottom:1px solid var(--line)">`;
    h += `<th style="padding:4px 6px">Service</th>`;
    h += `<th style="padding:4px 6px">Calls</th>`;
    h += `<th style="padding:4px 6px">Latency</th>`;
    h += `<th style="padding:4px 6px; width:35%">Share</th>`;
    h += `</tr></thead><tbody>`;
    (b.services || []).forEach(s => {
      const subOps = s.sub_operations || [];
      const opsStr = (!subOps.length) ? Object.entries(s.operations || {}).map(([op, n]) => `${op} (${n})`).join(', ') : '';
      const opsHint = opsStr ? `<br><span class="muted" style="font-size:11px">${esc(opsStr)}</span>` : '';
      h += `<tr style="border-bottom:1px solid rgba(255,255,255,0.06); background:rgba(255,255,255,0.015)">`;
      h += `<td style="padding:7px 6px"><strong>${esc(s.service)}</strong>${opsHint}</td>`;
      h += `<td style="padding:7px 6px; text-align:center"><strong>${esc(s.calls)}</strong></td>`;
      h += `<td style="padding:7px 6px; text-align:right"><strong>${esc(s.total_ms)} ms</strong></td>`;
      h += `<td style="padding:7px 6px">`;
      h += `<div style="display:flex; align-items:center; gap:8px">`;
      h += `<div style="flex:1; background:#21262d; border-radius:4px; height:8px; overflow:hidden">`;
      h += `<div style="width:${Math.min(100, s.percent)}%; background:var(--accent); height:8px"></div>`;
      h += `</div>`;
      h += `<span style="font-size:11px; width:36px; text-align:right">${esc(s.percent)}%</span>`;
      h += `</div></td>`;
      h += `</tr>`;
      subOps.forEach(sub => {
        const subPct = sub.percent_of_service || 0;
        h += `<tr style="border-bottom:1px solid rgba(255,255,255,0.03); color:var(--muted); font-size:11px">`;
        h += `<td style="padding:4px 6px 4px 18px"><span style="color:#58a6ff; opacity:0.8; margin-right:4px">↳</span> ${esc(sub.operation)}</td>`;
        h += `<td style="padding:4px 6px; text-align:center">${esc(sub.calls)}</td>`;
        h += `<td style="padding:4px 6px; text-align:right">${esc(sub.total_ms)} ms</td>`;
        h += `<td style="padding:4px 6px">`;
        h += `<div style="display:flex; align-items:center; gap:8px">`;
        h += `<div style="flex:1; background:#21262d; border-radius:3px; height:5px; overflow:hidden">`;
        h += `<div style="width:${Math.min(100, subPct)}%; background:#8b949e; height:5px"></div>`;
        h += `</div>`;
        h += `<span style="font-size:10px; width:36px; text-align:right">${esc(subPct)}%</span>`;
        h += `</div></td>`;
        h += `</tr>`;
      });
    });
    h += `</tbody></table></div>`;
  } else if (b) {
    h += `<p class="muted" style="font-size:12px; margin-top:12px">⚡ AWS Service Calls: <strong>0</strong> (Offline mock review — 0 ms AWS API latency).</p>`;
  }
  return h + '</div>';
}
</script>
</body>
</html>"""
