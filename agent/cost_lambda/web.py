"""
web.py — public "Cloud cost" web view for Reality Check.

Served by its OWN Lambda Function URL (auth NONE). Reads the rolled-up daily
cost history that the collector Lambda writes into Aurora and renders a small,
self-contained HTML page plus a JSON API.

Why a separate function from the offline demo: the public contract-review demo
(agent/web_handler.py) intentionally has ZERO network/DB privileges. This view
needs read-only Aurora access, so it lives in its own function with its own
least-privilege role (IAM-auth as the read-only `cost_reader` db user). Nothing
sensitive is exposed — only aggregate daily AWS spend for this demo account.

Connectivity: native PostgreSQL (psycopg) over TLS verify-full, IAM auth token
as the password. No stored secret.

Routes (Lambda Function URL, payload format 2.0):
  GET /            -> the cost page (HTML)
  GET /api/costs   -> recent daily costs as JSON
  GET /healthz     -> liveness
"""
from __future__ import annotations

import datetime as dt
import json
import os

import boto3
import certifi
import psycopg

DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "postgres")
DB_IAM_USER = os.environ.get("DB_READER_USER", "cost_reader")
REGION = os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_REGION", "us-east-1")
DAYS = int(os.environ.get("COST_WINDOW_DAYS", "30"))

_rds = boto3.client("rds", region_name=REGION)


def _connect() -> psycopg.Connection:
    token = _rds.generate_db_auth_token(
        DBHostname=DB_HOST, Port=DB_PORT, DBUsername=DB_IAM_USER, Region=REGION
    )
    return psycopg.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_IAM_USER,
        password=token, sslmode="verify-full", sslrootcert=certifi.where(),
        connect_timeout=15,
    )


def _fetch_costs() -> dict:
    cutoff = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=DAYS)).isoformat()
    rows = []
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT usage_date, unblended_usd, net_usd, currency, services "
                "FROM daily_cost WHERE usage_date >= %s ORDER BY usage_date DESC;",
                (cutoff,),
            )
            for d, unblended, net, cur_code, services in cur.fetchall():
                rows.append({
                    "date": d.isoformat(),
                    "unblended_usd": float(unblended),
                    "net_usd": float(net),
                    "currency": cur_code,
                    "services": services or {},
                })
    window_unblended = round(sum(r["unblended_usd"] for r in rows), 4)
    window_net = round(sum(r["net_usd"] for r in rows), 4)
    # Roll up spend by service across the window.
    by_service: dict[str, float] = {}
    for r in rows:
        for svc, amt in r["services"].items():
            by_service[svc] = round(by_service.get(svc, 0.0) + float(amt), 6)
    top_services = sorted(by_service.items(), key=lambda kv: kv[1], reverse=True)[:10]
    return {
        "window_days": DAYS,
        "days_recorded": len(rows),
        "window_unblended_usd": window_unblended,
        "window_net_usd": window_net,
        "currency": rows[0]["currency"] if rows else "USD",
        "daily": rows,
        "top_services": [{"service": s, "usd": round(v, 4)} for s, v in top_services],
    }


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
    return (rc.get("method") or "GET").upper(), (rc.get("path") or event.get("rawPath") or "/")


def lambda_handler(event, context=None):
    method, path = _method_path(event)
    if path in ("/favicon.ico", "/robots.txt"):
        return {"statusCode": 404, "headers": {"cache-control": "no-store"}, "body": ""}
    try:
        if path == "/healthz":
            return _json(200, {"ok": True, "view": "cloud-cost"})
        if path == "/api/costs":
            return _json(200, _fetch_costs())
        if path in ("/", "/index.html"):
            return _html(200, _render(_fetch_costs()))
        return _json(404, {"error": f"not found: {method} {path}"})
    except Exception as exc:  # last-resort guard
        return _json(500, {"error": "internal error", "detail": str(exc)})


def _render(data: dict) -> str:
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    rows = "".join(
        f"<tr><td>{esc(r['date'])}</td>"
        f"<td class='num'>${r['unblended_usd']:.4f}</td>"
        f"<td class='num'>${r['net_usd']:.4f}</td></tr>"
        for r in data["daily"]
    ) or "<tr><td colspan='3' class='muted'>No cost data recorded yet.</td></tr>"

    svc_rows = "".join(
        f"<tr><td>{esc(s['service'])}</td><td class='num'>${s['usd']:.4f}</td></tr>"
        for s in data["top_services"]
    ) or "<tr><td colspan='2' class='muted'>No service spend in this window.</td></tr>"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reality Check — Cloud cost</title>
<style>
  :root {{ --bg:#0d1117; --card:#161b22; --line:#30363d; --fg:#e6edf3;
          --muted:#9da7b3; --accent:#58a6ff; --green:#3fb950; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
         font:15px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }}
  header, main {{ max-width:900px; margin:0 auto; padding:0 20px; }}
  header {{ padding-top:28px; }} h1 {{ margin:0 0 4px; font-size:24px; }}
  .tag {{ color:var(--muted); margin:0 0 6px; }}
  a {{ color:var(--accent); }}
  .cards {{ display:flex; flex-wrap:wrap; gap:14px; margin:18px 0; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:16px 18px; flex:1; min-width:200px; }}
  .big {{ font-size:28px; font-weight:700; color:var(--green); }}
  table {{ width:100%; border-collapse:collapse; margin:10px 0 26px; }}
  th, td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); }}
  th {{ color:var(--muted); font-weight:600; font-size:13px; }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .muted {{ color:var(--muted); }}
  .foot {{ color:var(--muted); font-size:12px; margin:30px 0 60px; }}
</style></head>
<body>
<header>
  <h1>Reality Check <span class="muted">— Cloud cost</span></h1>
  <p class="tag">Holistic daily AWS spend for this demo account, collected once a
  day and stored in Aurora Serverless v2 (PostgreSQL).</p>
  <p class="muted"><a href="/">&larr; Back to the contract-review demo</a></p>
</header>
<main>
  <div class="cards">
    <div class="card"><div class="muted">Last {esc(data['window_days'])} days (unblended)</div>
      <div class="big">${data['window_unblended_usd']:.2f}</div></div>
    <div class="card"><div class="muted">Last {esc(data['window_days'])} days (net, post-credit)</div>
      <div class="big">${data['window_net_usd']:.2f}</div></div>
    <div class="card"><div class="muted">Days recorded</div>
      <div class="big">{esc(data['days_recorded'])}</div></div>
  </div>

  <h3>Top services ({esc(data['window_days'])}-day roll-up)</h3>
  <table><thead><tr><th>Service</th><th class="num">Unblended</th></tr></thead>
  <tbody>{svc_rows}</tbody></table>

  <h3>Daily history</h3>
  <table><thead><tr><th>Date (UTC)</th><th class="num">Unblended</th>
  <th class="num">Net</th></tr></thead><tbody>{rows}</tbody></table>

  <p class="foot">Collected daily by an AWS Lambda (Graviton) that reads Cost
  Explorer and writes to Aurora via IAM database authentication — no stored
  secrets. This page reads the same table through a read-only IAM role.
  <a href="/api/costs">Raw JSON</a>.</p>
</main>
</body></html>"""
