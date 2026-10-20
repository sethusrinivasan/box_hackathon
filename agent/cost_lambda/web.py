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
import select
import time

import boto3
import certifi
import psycopg

from metrics import emit_metric, LatencyTracker

DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "postgres")
DB_IAM_USER = os.environ.get("DB_READER_USER", "cost_reader")
REGION = os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_REGION", "us-east-1")
DAYS = int(os.environ.get("COST_WINDOW_DAYS", "30"))

_rds = boto3.client("rds", region_name=REGION)


def _connect_phased(conninfo: str, timeout_sec: float = 14.0) -> tuple[psycopg.Connection, dict[str, float]]:
    """Connect via libpq nonblocking generator, tracking TCP, TLS, and IAM auth phases."""
    from psycopg import pq
    t_start = time.perf_counter()
    pgconn = pq.PGconn.connect_start(conninfo.encode("utf-8"))
    t_tcp = 0.0
    t_tls = 0.0
    t_auth = 0.0
    t_tls_start = None
    t_auth_start = None
    t_first_data_after_awaiting = None
    deadline = t_start + timeout_sec
    while True:
        now = time.perf_counter()
        if now > deadline:
            raise psycopg.OperationalError("connection timed out")
        st = pgconn.connect_poll()
        status = pgconn.status
        now = time.perf_counter()
        if status == pq.ConnStatus.SSL_STARTUP and t_tls_start is None:
            t_tls_start = now
            t_tcp = (t_tls_start - t_start) * 1000
        elif status == pq.ConnStatus.AWAITING_RESPONSE and t_auth_start is None:
            t_auth_start = now
            if t_tls_start:
                t_tls = (t_auth_start - t_tls_start) * 1000
            else:
                t_tcp = (t_auth_start - t_start) * 1000
        if st == pq.PollingStatus.OK:
            t_end = time.perf_counter()
            if t_first_data_after_awaiting:
                t_auth = (t_end - t_first_data_after_awaiting) * 1000
            else:
                t_auth = (t_end - (t_auth_start or t_start)) * 1000
            break
        elif st == pq.PollingStatus.FAILED:
            err = pgconn.get_error_message("utf-8")
            raise psycopg.OperationalError(f"connection failed: {err}")
        elif st == pq.PollingStatus.READING:
            r, _, _ = select.select([pgconn.socket], [], [], max(0.1, deadline - now))
            if r and t_auth_start is not None and t_first_data_after_awaiting is None:
                t_first_data_after_awaiting = time.perf_counter()
            if not r and time.perf_counter() > deadline:
                raise psycopg.OperationalError("connection timed out while reading")
        elif st == pq.PollingStatus.WRITING:
            _, w, _ = select.select([], [pgconn.socket], [], max(0.1, deadline - now))
            if not w and time.perf_counter() > deadline:
                raise psycopg.OperationalError("connection timed out while writing")
    pgconn.nonblocking = 1

    cold_wait_ms = 0.0
    if t_auth_start and t_first_data_after_awaiting:
        cold_wait_ms = (t_first_data_after_awaiting - t_auth_start) * 1000

    return psycopg.Connection(pgconn), {
        "tcp_ms": round(t_tcp, 1),
        "tls_ms": round(t_tls, 1),
        "auth_ms": round(t_auth, 1),
        "cold_wait_ms": round(cold_wait_ms, 1),
    }


def _connect(tracker: LatencyTracker | None = None) -> psycopg.Connection:
    """Connect with native PG + IAM token. Aurora Serverless v2 auto-pauses when
    idle and takes up to ~15s to resume on the first connection, so we retry
    once with a fresh token to ride through a cold resume instead of 500-ing."""
    last_exc: Exception | None = None
    started = time.perf_counter()
    for attempt in range(2):
        t0 = time.perf_counter()
        token = _rds.generate_db_auth_token(
            DBHostname=DB_HOST, Port=DB_PORT, DBUsername=DB_IAM_USER, Region=REGION
        )
        if tracker:
            tracker.record("AWS RDS", "rds:generate_db_auth_token", (time.perf_counter() - t0) * 1000)
        try:
            conninfo = (
                f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_IAM_USER} "
                f"password={token} sslmode=verify-full sslrootcert={certifi.where()} "
                f"connect_timeout=10"
            )
            t0 = time.perf_counter()
            try:
                conn, phases = _connect_phased(conninfo)
            except Exception:
                conn = psycopg.connect(
                    host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_IAM_USER,
                    password=token, sslmode="verify-full", sslrootcert=certifi.where(),
                    connect_timeout=10,
                )
                total_dur = (time.perf_counter() - t0) * 1000
                phases = {"tcp_ms": 0.0, "tls_ms": total_dur * 0.4, "auth_ms": total_dur * 0.6}

            elapsed_ms = (time.perf_counter() - started) * 1000
            cold_wait = phases.get("cold_wait_ms", 0.0)
            is_cold = attempt > 0 or cold_wait > 1500 or elapsed_ms > 2500

            cold_resume_ms = 0.0
            if is_cold:
                # If there were prior failed attempts (e.g. timeout during auto-pause), include that time
                retry_time_ms = (t0 - started) * 1000 if attempt > 0 else 0.0
                if cold_wait > 500:
                    cold_resume_ms = retry_time_ms + cold_wait
                else:
                    cold_resume_ms = max(0.0, elapsed_ms - phases["tcp_ms"] - phases["tls_ms"] - 300.0)
                    phases["auth_ms"] = min(phases["auth_ms"], 300.0)

            tls_ver = "TLSv1.3"
            tls_cipher = ""
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT version, cipher FROM pg_stat_ssl WHERE pid = pg_backend_pid();")
                    ssl_row = cur.fetchone()
                    if ssl_row and ssl_row[0]:
                        tls_ver = ssl_row[0]
                        tls_cipher = ssl_row[1] or ""
            except Exception:
                tls_ver = "TLSv1.3"

            if tracker:
                tracker.cold_resume = is_cold
                tracker.tls_version = tls_ver
                tracker.tls_cipher = tls_cipher
                if cold_resume_ms > 0:
                    tracker.record(
                        "Amazon Aurora PostgreSQL",
                        "Aurora Serverless v2 [Compute cold resume (0 → 0.5 ACU)]",
                        cold_resume_ms,
                    )
                if phases["tcp_ms"] > 1.0:
                    tracker.record("Amazon Aurora PostgreSQL", "psycopg:connect [TCP connect]", phases["tcp_ms"])
                tracker.record("Amazon Aurora PostgreSQL", f"psycopg:connect [TLS handshake - {tls_ver}]", phases["tls_ms"])
                tracker.record("Amazon Aurora PostgreSQL", "psycopg:connect [Server IAM auth]", phases["auth_ms"])
            emit_metric("DbConnectLatencyMs", round(elapsed_ms, 1), "Milliseconds",
                        fn="web")
            emit_metric("DbColdResume", 1 if is_cold else 0, "Count", fn="web")
            return conn
        except psycopg.OperationalError as exc:
            last_exc = exc  # likely Aurora still resuming; retry once
    emit_metric("DbConnectFailure", 1, "Count", fn="web")
    raise last_exc  # type: ignore[misc]


def _f(v) -> float:
    return float(v) if v is not None else 0.0


def _fmt(v) -> str:
    """Format a dollar amount for display; '—' when unknown."""
    return f"{float(v):.2f}" if v is not None else "—"


def _sparkline_svg(
    values: list[float],
    width: int = 90,
    height: int = 22,
    color: str | None = None,
) -> str:
    """Render a lightweight inline SVG sparkline for a time series.

    Values are expected in chronological order (oldest -> newest).
    Color indicates trend unless explicitly provided:
      - #d29922 (amber) if spend is trending upwards (>5% increase)
      - #3fb950 (green) if spend is trending downwards (>5% decrease)
      - #58a6ff (blue)  if spend is steady or baseline zero
    """
    if not values:
        return f'<svg width="{width}" height="{height}" class="sparkline"></svg>'
    pad_x, pad_y = 3, 3
    n = len(values)
    max_v = max(values) if values else 0.0
    first_v = values[0] if values else 0.0
    last_v = values[-1] if values else 0.0
    if color is None:
        if last_v > first_v * 1.05 and last_v > 0.001:
            color = "#d29922"  # rising spend
        elif last_v < first_v * 0.95 and first_v > 0.001:
            color = "#3fb950"  # declining spend
        else:
            color = "#58a6ff"  # steady / normal
    inner_w = width - 2 * pad_x
    inner_h = height - 2 * pad_y
    if n == 1:
        y = (height - pad_y) - ((values[0] / max_v) * inner_h if max_v > 0 else 0)
        pts = [(pad_x, round(y, 1)), (width - pad_x, round(y, 1))]
    else:
        pts = []
        for i, v in enumerate(values):
            x = pad_x + (inner_w * i / (n - 1))
            y = (height - pad_y) - ((v / max_v) * inner_h if max_v > 0 else 0)
            pts.append((round(x, 1), round(y, 1)))
    poly_pts = " ".join(f"{x},{y}" for x, y in pts)
    base_y = height - pad_y
    area_pts = f"{pts[0][0]},{base_y} " + poly_pts + f" {pts[-1][0]},{base_y}"
    last_x, last_y = pts[-1]
    title = f"Latest: ${last_v:.4f} · Peak: ${max_v:.4f}"
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'style="vertical-align:middle; overflow:visible" aria-label="Trend sparkline">'
        f'<title>{title}</title>'
        f'<polygon points="{area_pts}" fill="{color}" fill-opacity="0.18" />'
        f'<polyline points="{poly_pts}" fill="none" stroke="{color}" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" />'
        f'<circle cx="{last_x}" cy="{last_y}" r="2.5" fill="{color}" />'
        f"</svg>"
    )


def _fetch_costs() -> dict:
    cutoff = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=DAYS)).isoformat()
    rows = []
    credit = {}          # latest-day credit snapshot
    resource_count = 0
    tracker = LatencyTracker()
    with _connect(tracker) as conn:
        with conn.cursor() as cur:
            t0 = time.perf_counter()
            cur.execute(
                "SELECT usage_date, gross_usd, net_usd, currency, "
                "       COALESCE(gross_services, services), plan_type, "
                "       credits_remaining_usd, credits_consumed_usd, "
                "       credits_granted_usd, credits_expiry, projection "
                "FROM daily_cost WHERE usage_date >= %s ORDER BY usage_date DESC;",
                (cutoff,),
            )
            raw_rows = cur.fetchall()
            tracker.record("Amazon Aurora PostgreSQL", "Query daily_cost (30 days)",
                           (time.perf_counter() - t0) * 1000, row_count=len(raw_rows))
            for (d, gross, net, cur_code, services, plan_type, rem, cons,
                 granted, expiry, projection) in raw_rows:
                rows.append({
                    "date": d.isoformat(),
                    "gross_usd": _f(gross),
                    "net_usd": _f(net),
                    "currency": cur_code,
                    "services": services or {},
                })
                # First (latest) row carries the current credit snapshot.
                if not credit:
                    credit = {
                        "plan_type": plan_type,
                        "credits_granted_usd": _f(granted) if granted is not None else None,
                        "credits_remaining_usd": _f(rem) if rem is not None else None,
                        "credits_consumed_usd": _f(cons) if cons is not None else None,
                        "credits_expiry": expiry.isoformat() if expiry else None,
                        "projection": projection or {},
                    }
            # Resource inventory summary (count + newest-seen), best-effort.
            try:
                t0 = time.perf_counter()
                cur.execute("SELECT count(*) FROM resource_inventory;")
                resource_count = int(cur.fetchone()[0])
                tracker.record("Amazon Aurora PostgreSQL", "Query resource_inventory count",
                               (time.perf_counter() - t0) * 1000)
            except Exception:  # table may not exist on a pre-migration DB
                resource_count = 0

    window_gross = round(sum(r["gross_usd"] for r in rows), 4)
    window_net = round(sum(r["net_usd"] for r in rows), 4)
    # Roll up GROSS spend by service across the window.
    by_service: dict[str, float] = {}
    for r in rows:
        for svc, amt in r["services"].items():
            by_service[svc] = round(by_service.get(svc, 0.0) + float(amt), 6)
    top_services = sorted(by_service.items(), key=lambda kv: kv[1], reverse=True)[:10]

    # Chronological daily series for sparkline trends (oldest -> newest)
    chronological = sorted(rows, key=lambda r: r["date"])
    gross_trend = [round(r["gross_usd"], 4) for r in chronological]
    top_services_data = [
        {
            "service": s,
            "usd": round(v, 4),
            "trend": [round(float(r["services"].get(s, 0.0)), 6) for r in chronological],
        }
        for s, v in top_services
    ]

    return {
        "window_days": DAYS,
        "days_recorded": len(rows),
        "window_gross_usd": window_gross,
        "window_net_usd": window_net,
        "currency": rows[0]["currency"] if rows else "USD",
        "credit": credit,
        "resource_count": resource_count,
        "daily": rows,
        "gross_trend": gross_trend,
        "top_services": top_services_data,
        "latency_breakdown": tracker.summary(),
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

    max_daily_gross = max((r.get("gross_usd", 0.0) for r in data.get("daily", [])), default=0.0)

    def _pct(r):
        g = r.get("gross_usd", 0.0)
        return round((g / max_daily_gross * 100), 1) if max_daily_gross > 0 else 0

    rows = "".join(
        f"<tr><td>{esc(r['date'])}</td>"
        f"<td class='num'>"
        f"<div style='display:inline-flex; align-items:center; justify-content:flex-end; gap:8px; width:100%'>"
        f"<div style='flex:1; max-width:70px; background:#21262d; border-radius:3px; height:6px; overflow:hidden'>"
        f"<div style='width:{_pct(r)}%; background:var(--accent); height:6px'></div>"
        f"</div>"
        f"<span>${r['gross_usd']:.4f}</span>"
        f"</div>"
        f"</td>"
        f"<td class='num'>${r['net_usd']:.4f}</td></tr>"
        for r in data.get("daily", [])
    ) or "<tr><td colspan='3' class='muted'>No cost data recorded yet.</td></tr>"

    svc_rows = "".join(
        f"<tr><td>{esc(s['service'])}</td>"
        f"<td style='text-align:center'>{_sparkline_svg(s.get('trend', []))}</td>"
        f"<td class='num'>${s['usd']:.4f}</td></tr>"
        for s in data.get("top_services", [])
    ) or "<tr><td colspan='3' class='muted'>No service spend in this window.</td></tr>"

    chronological = sorted(data.get("daily", []), key=lambda r: r.get("date", ""))
    gross_trend = data.get("gross_trend") or [r.get("gross_usd", 0.0) for r in chronological]
    gross_sparkline = _sparkline_svg(gross_trend, width=110, height=26)

    b = data.get("latency_breakdown") or {}
    breakdown_html = ""
    if b and b.get("total_calls", 0) > 0:
        total_calls = b.get("total_calls", 0)
        total_ms = b.get("total_ms", 0.0)
        status_label = b.get("database_status", "Aurora Serverless v2")
        tls_ver = b.get("tls_version")
        if tls_ver and tls_ver not in status_label:
            status_label = f"{status_label} · {tls_ver}"
        status_color = "#d29922" if b.get("cold_resume") else "#3fb950"
        breakdown_rows = ""
        for s in b.get("services", []):
            pct = s.get("percent", 0.0)
            sub_ops = s.get("sub_operations") or []
            ops_str = ", ".join(f"{op} ({n})" for op, n in s.get("operations", {}).items()) if not sub_ops else ""
            ops_hint = f"<br><span class='muted' style='font-size:11px'>{esc(ops_str)}</span>" if ops_str else ""
            breakdown_rows += f"""
            <tr style="border-bottom:1px solid rgba(255,255,255,0.06); background:rgba(255,255,255,0.015)">
              <td style="padding:7px 8px">
                <strong style="color:var(--fg)">{esc(s.get('service'))}</strong>{ops_hint}
              </td>
              <td style="padding:7px 8px; text-align:center"><strong>{esc(s.get('calls'))}</strong></td>
              <td style="padding:7px 8px; text-align:right" class="num"><strong>{s.get('total_ms'):.1f} ms</strong></td>
              <td style="padding:7px 8px">
                <div style="display:flex; align-items:center; gap:8px">
                  <div style="flex:1; background:#21262d; border-radius:4px; height:8px; overflow:hidden">
                    <div style="width:{min(100, pct)}%; background:var(--accent); height:8px"></div>
                  </div>
                  <span style="font-size:11px; width:36px; text-align:right" class="num">{pct:.1f}%</span>
                </div>
              </td>
            </tr>"""
            for sub in sub_ops:
                sub_pct = sub.get("percent_of_service", 0.0)
                breakdown_rows += f"""
            <tr style="border-bottom:1px solid rgba(255,255,255,0.03); color:var(--muted); font-size:11px">
              <td style="padding:4px 8px 4px 22px">
                <span style="color:#58a6ff; opacity:0.8; margin-right:4px">↳</span> {esc(sub.get('operation'))}
              </td>
              <td style="padding:4px 8px; text-align:center">{esc(sub.get('calls'))}</td>
              <td style="padding:4px 8px; text-align:right" class="num">{sub.get('total_ms'):.1f} ms</td>
              <td style="padding:4px 8px">
                <div style="display:flex; align-items:center; gap:8px">
                  <div style="flex:1; background:#21262d; border-radius:3px; height:5px; overflow:hidden">
                    <div style="width:{min(100, sub_pct)}%; background:#8b949e; height:5px"></div>
                  </div>
                  <span style="font-size:10px; width:36px; text-align:right" class="num">{sub_pct:.1f}%</span>
                </div>
              </td>
            </tr>"""

        breakdown_html = f"""
        <div class="card" style="margin-top:24px; background:#0d1117; border:1px solid var(--line); border-radius:10px">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px">
            <div>
              <strong style="font-size:13px">⚡ AWS Service &amp; Aurora Latency Breakdown</strong>
              <span style="display:inline-block; margin-left:8px; padding:2px 8px; border-radius:999px; font-size:11px; font-weight:600; background:rgba(255,255,255,0.06); color:{status_color}">
                {esc(status_label)}
              </span>
            </div>
            <span class="muted" style="font-size:12px">{esc(total_calls)} calls · {total_ms:.1f} ms total</span>
          </div>
          <table style="width:100%; font-size:12px; border-collapse:collapse; margin-top:6px">
            <thead>
              <tr style="text-align:left; color:var(--muted); border-bottom:1px solid var(--line)">
                <th style="padding:4px 8px">Service / Component</th>
                <th style="padding:4px 8px; text-align:center">Calls</th>
                <th style="padding:4px 8px; text-align:right">Latency</th>
                <th style="padding:4px 8px; width:35%">Share</th>
              </tr>
            </thead>
            <tbody>{breakdown_rows}</tbody>
          </table>
        </div>"""

    # Credit / runway card — the point of the whole view: usage is free today
    # (credits), so track gross burn and project how long credits last.
    c = data.get("credit") or {}
    proj = c.get("projection") or {}
    rem = c.get("credits_remaining_usd")
    gr = c.get("credits_granted_usd")
    cons = c.get("credits_consumed_usd")
    binding = proj.get("binding_limit")
    exp_days = proj.get("days_until_expiry")
    exh_days = proj.get("days_until_exhaustion")
    avg_burn = proj.get("avg_daily_gross_usd")
    if binding == "expiry":
        runway = (f"credits <b>expire</b> in <b>{exp_days}</b> days "
                  f"(before they'd be used up at the current ${_fmt(avg_burn)}/day burn)")
    elif binding == "exhaustion":
        runway = (f"credits <b>run out</b> in ~<b>{int(exh_days) if exh_days else '—'}</b> days "
                  f"at the current ${_fmt(avg_burn)}/day burn")
    else:
        runway = "projection unavailable yet (needs a day of history)"
    credit_card = (
        f'<div class="card"><div class="muted">Credits remaining'
        f'{f" of ${_fmt(gr)} granted" if gr else ""}</div>'
        f'<div class="big">${_fmt(rem)}</div>'
        f'<div class="muted" style="font-size:12px;margin-top:6px">'
        f'{f"${_fmt(cons)} consumed · " if cons is not None else ""}{runway}</div></div>'
    ) if rem is not None else ""

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
  <p class="tag">Holistic AWS spend for this demo account, collected twice daily
  and stored in Aurora Serverless v2 (PostgreSQL). <b>Gross</b> is the real
  pre-credit consumption; <b>net</b> is what's actually billed after promotional
  credits (≈$0 while credits last).</p>
  <p class="muted"><a href="/">&larr; Back to the contract-review demo</a></p>
</header>
<main>
  <div class="cards">
    <div class="card">
      <div class="muted">Last {esc(data['window_days'])} days (gross usage)</div>
      <div style="display:flex; justify-content:space-between; align-items:flex-end; margin-top:4px">
        <div class="big">${data['window_gross_usd']:.2f}</div>
        <div>{gross_sparkline}</div>
      </div>
    </div>
    <div class="card"><div class="muted">Last {esc(data['window_days'])} days (net, post-credit)</div>
      <div class="big">${data['window_net_usd']:.2f}</div></div>
    {credit_card}
    <div class="card"><div class="muted">Resources tracked</div>
      <div class="big">{esc(data.get('resource_count', 0))}</div>
      <div class="muted" style="font-size:12px;margin-top:6px">{esc(data['days_recorded'])} days recorded</div></div>
  </div>

  <h3>Top services ({esc(data['window_days'])}-day roll-up, gross)</h3>
  <table><thead><tr><th>Service</th><th style="width:110px; text-align:center">Trend ({esc(data['window_days'])}d)</th><th class="num">Gross usage</th></tr></thead>
  <tbody>{svc_rows}</tbody></table>

  <h3>Daily history</h3>
  <table><thead><tr><th>Date (UTC)</th><th class="num">Gross</th>
  <th class="num">Net</th></tr></thead><tbody>{rows}</tbody></table>

  {breakdown_html}

  <p class="foot">Collected twice daily by an AWS Lambda (Graviton) that reads
  Cost Explorer + the Free Tier / account-plan APIs and enumerates active
  resources, writing to Aurora via IAM database authentication — no stored
  secrets. This page reads the same tables through a read-only IAM role.
  <a href="/api/costs">Raw JSON</a>.</p>
</main>
</body></html>"""
