"""
web_handler.py — public, no-auth web demo for Reality Check (contract claim review).

Served by an AWS Lambda Function URL (see agent/infra/cdk/.../web_demo_stack.py).
Renders one self-contained HTML page and a tiny JSON API that runs the OFFLINE
contract-review pipeline (agent/local_agent.py) against the bundled synthetic
fixtures in docs/box_upload/ — reviewing a draft sales contract's claims against
Box "evidence" documents and returning a claim-by-claim verdict.

Why this is safe to expose publicly with no auth:
  * OFFLINE ONLY. We use LocalDocs (reads docs/box_upload/*) and a CANNED
    Bedrock client — no live Box, no real Bedrock call, no credentials in the
    function env. There is nothing sensitive to leak and no per-request cost
    beyond Lambda compute.
  * All data is synthetic (fictional customer "Northstar", fictional evidence).
  * Report PDFs/comments are written to /tmp only (Lambda's writable scratch)
    via REALITY_CHECK_REPORTS_DIR; never anywhere durable.
  * The CDK stack caps blast radius with reserved concurrency.

Routes (Lambda Function URL, payload format 2.0):
  GET  /                -> the demo page (HTML)
  POST /api/run         -> review a contract; JSON in, JSON out
  GET  /report.pdf?...  -> the generated risk-report PDF
  GET  /healthz         -> liveness probe (JSON)
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time

# ---------------------------------------------------------------------------
# Force OFFLINE mode before importing the agent. local_agent.get_docs() returns
# BoxDocs only when all six Box env vars are set; blanking them (present-but-
# empty, so the .env auto-loader can't re-populate) guarantees LocalDocs. We
# also route report writes to /tmp (the Lambda fs is read-only except /tmp).
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

import local_agent as la          # noqa: E402 — the post-restructure runtime
import feature_flags as ff        # noqa: E402 — SSM-backed demo feature flag
from cost_estimate import estimate_cost  # noqa: E402 — live-path cost heads-up
from telemetry import (           # noqa: E402 — re-wire the shared observability
    get_logger, log_event, emit_metric, set_request_id,
)

# Model the live path WOULD use (frugal default). Shown in cost estimates only;
# the hosted demo never actually calls it.
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
    """LocalDocs, but reports/comments go to a writable dir (/tmp in Lambda)."""

    def __init__(self):
        super().__init__()
        self.reports = _REPORTS_DIR


# Contracts the demo exposes (fixed allow-list, not arbitrary input).
_CONTRACTS = {
    "northstar": "Northstar-Archive-Migration-Agreement",
    "northstar-v2": "Northstar-Archive-Migration-Agreement-v2",
}


def _estimate_live_cost(contract_key: str, out: dict) -> dict:
    """Estimate what a REAL Bedrock run of this review would have cost. We
    reconstruct the exact input the live path sends the model — the contract
    text plus every evidence PDF's text plus the system prompt — so the estimate
    reflects reality. Hosted demo never calls Bedrock; this is a transparency
    heads-up on the live economics."""
    claims = out.get("claims", []) or []
    docs = _TmpDocs()
    # Same text assembly the live llm_review() feeds the model.
    parts = [getattr(la, "REVIEW_SYSTEM", "")]
    name = _CONTRACTS.get(contract_key, _CONTRACTS["northstar"])
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
    name = _CONTRACTS.get(contract_key, _CONTRACTS["northstar"])
    # Honor the feature flag: when evidence-store is ON, a real deployment's
    # review path uses pgvector semantic retrieval (see docs/DESIGN_pgvector_*).
    # The hosted demo is canned/offline with no DB, so here the flag drives the
    # reported retrieval mode rather than live retrieval — but we set the env so
    # the same code path behaves correctly if ever run with DB access.
    retrieval_on = ff.is_enabled()
    os.environ["EVIDENCE_STORE"] = "on" if retrieval_on else "off"
    out = la.review_contract(_TmpDocs(), name, _CannedBedrock())
    s = out.get("summary", {})
    emit_metric("WebDemoReview", 1, "Count", contract=contract_key)
    emit_metric("WebDemoRisksFound", s.get("Risk identified", 0), "Count")
    emit_metric("WebDemoEvidenceMode", 1, "Count",
                mode="pgvector" if retrieval_on else "full-corpus")
    result = {
        "contract": out.get("contract"),
        "summary": s,
        "claims": out.get("claims", []),
        "sources": out.get("sources", []),
        "report_name": out.get("report_name"),
        "comments_posted": out.get("comments_posted_on_contract"),
        "mode": "mock",
        "evidence_retrieval": "pgvector (semantic top-k)" if retrieval_on
                               else "full-corpus",
    }
    if live:
        # User asked for a live run: we still return the MOCK result, attach the
        # estimated real-run cost, and tell them how to run it for real.
        result["mode"] = "mock (live requested)"
        result["live_cost_estimate"] = _estimate_live_cost(contract_key, out)
        result["live_notice"] = (
            "This hosted demo always runs a mock (offline, $0). The estimate "
            "above is what one real Bedrock review would cost on the configured "
            "model. For a real run, clone the repo and supply your own AWS "
            "credentials: https://github.com/sethusrinivasan/box_hackathon"
        )
        emit_metric("WebDemoLiveRequested", 1, "Count", contract=contract_key)
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
        if path in ("/", "/index.html") and method == "GET":
            status = 200
            return _html(200, _page_html())
        if path == "/healthz":
            status = 200
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
        if path == "/report.pdf" and method == "GET":
            qs = event.get("queryStringParameters") or {}
            contract = (qs.get("contract") or "northstar").strip()
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
    """Build the risk-report PDF for a contract and return it inline (base64).
    Rebuilds via the agent's build_report so no disk write is needed."""
    name = _CONTRACTS.get(contract_key, _CONTRACTS["northstar"])
    docs = _TmpDocs()
    contracts = docs.contracts()
    contract = la.match_contract(contracts, name)
    if not contract:
        return _json(404, {"error": f"no contract '{contract_key}'"})
    sources = []
    for i, e in enumerate(docs.evidence(), 1):
        text = la.pdf_text(docs.read(e["id"]))
        title = text.strip().splitlines()[0] if text.strip() else e["name"]
        sources.append({**e, "ref": f"B{i}", "text": text, "title": title,
                        "last_updated": la.last_updated(text)})
    claims = la.by_severity(la.classify(_CANNED_CLAIMS))
    counts = la.summarize(claims)
    pdf = la.build_report(contract, claims, counts, sources)
    emit_metric("WebDemoPdf", 1, "Count", contract=contract_key)
    return {"statusCode": 200,
            "headers": {"content-type": "application/pdf",
                        "content-disposition": f'inline; filename="RealityCheck-{contract_key}.pdf"',
                        "cache-control": "no-store"},
            "isBase64Encoded": True,
            "body": base64.b64encode(pdf).decode("ascii")}


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
  h += `<p class="muted" style="margin-top:12px">A comment was posted on every claim, on the contract in Box (${esc(r.comments_posted)} comments).</p>`;
  h += `<p><a href="/report.pdf?contract=${esc(key)}" target="_blank">Open the risk-report PDF →</a></p>`;
  return h + '</div>';
}
</script>
</body>
</html>"""
