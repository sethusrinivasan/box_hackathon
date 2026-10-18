"""
test_web_handler.py — tests for the public web demo handler (post-restructure).

Exercises agent/web_handler.py the way the live Lambda Function URL is called:
HTTP-shaped events in, HTTP responses out. Verifies routing, the fixed contract
allow-list, the PDF route, offline enforcement, favicon suppression, and that
one correlation id threads every log line of a request.

Run:
    python3 agent/test_web_handler.py
    python3 -m pytest agent/test_web_handler.py
"""
import base64
import json
import os
import sys

# Simulate leaked Box creds in the env BEFORE import — the handler must
# neutralize them so the public endpoint can never go live.
for _k in ("BOX_CLIENT_ID", "BOX_CLIENT_SECRET", "BOX_ENTERPRISE_ID",
           "BOX_CONTRACTS_FOLDER_ID", "BOX_EVIDENCE_FOLDER_ID", "BOX_REPORTS_FOLDER_ID"):
    os.environ[_k] = "leaked"
os.environ["BOX_SECRET_ARN"] = "arn:aws:secretsmanager:us-east-2:123:secret:x"
os.environ.setdefault("REALITY_CHECK_REPORTS_DIR", "/tmp/rc-test-reports")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
for p in (HERE, os.path.join(ROOT, "box")):
    sys.path.insert(0, p)

import web_handler as web  # noqa: E402


def _ev(method, path, body=None, qs=None):
    e = {"requestContext": {"http": {"method": method, "path": path, "sourceIp": "203.0.113.9"}}}
    if body is not None:
        e["body"] = json.dumps(body)
    if qs is not None:
        e["queryStringParameters"] = qs
    return e


def test_offline_enforced_despite_env_creds():
    # The handler blanks Box creds + BOX_SECRET_ARN at import, so a review runs
    # offline (LocalDocs + canned client) and returns 200, never a live attempt.
    assert os.environ.get("BOX_CLIENT_ID") == ""
    assert os.environ.get("BOX_SECRET_ARN") is None
    r = web.lambda_handler(_ev("POST", "/api/run", {"contract": "northstar"}))
    assert r["statusCode"] == 200, r["body"]


def test_landing_page():
    r = web.lambda_handler(_ev("GET", "/"))
    assert r["statusCode"] == 200 and "text/html" in r["headers"]["content-type"]
    assert "Reality Check" in r["body"]


def test_healthz():
    r = web.lambda_handler(_ev("GET", "/healthz"))
    body = json.loads(r["body"])
    assert r["statusCode"] == 200 and body["ok"] is True and body["mode"] == "offline"
    assert "northstar" in body["contracts"]


def test_review_returns_claims_and_summary():
    r = web.lambda_handler(_ev("POST", "/api/run", {"contract": "northstar"}))
    res = json.loads(r["body"])["result"]
    assert res["contract"].startswith("Northstar")
    assert len(res["claims"]) == 5
    assert res["summary"]["Risk identified"] == 3
    assert res["comments_posted"] == 5
    # every claim carries the fields the UI renders
    for c in res["claims"]:
        assert c["location"] and c["result"] and c["severity"] in ("High", "Medium", "None")


def test_both_contracts_resolve():
    for key in ("northstar", "northstar-v2"):
        r = web.lambda_handler(_ev("POST", "/api/run", {"contract": key}))
        assert r["statusCode"] == 200, (key, r["body"])


def test_unknown_contract_falls_back_safely():
    # Unknown key defaults to the primary contract rather than erroring.
    r = web.lambda_handler(_ev("POST", "/api/run", {"contract": "../etc/passwd"}))
    assert r["statusCode"] == 200
    assert json.loads(r["body"])["result"]["contract"].startswith("Northstar")


def test_report_pdf_streams_bytes():
    r = web.lambda_handler(_ev("GET", "/report.pdf", qs={"contract": "northstar"}))
    assert r["statusCode"] == 200
    assert r["headers"]["content-type"] == "application/pdf"
    assert r["isBase64Encoded"] is True
    assert base64.b64decode(r["body"]).startswith(b"%PDF-")


def test_favicon_quietly_404():
    r = web.lambda_handler(_ev("GET", "/favicon.ico"))
    assert r["statusCode"] == 404 and r["body"] == ""


def test_unknown_route_404():
    assert web.lambda_handler(_ev("GET", "/nope"))["statusCode"] == 404


def test_correlation_id_threads_across_loggers():
    # web.request / web.response are INFO; make sure they're emitted, and
    # re-point the logger's handler at our buffer (it was bound to the real
    # stderr at import time).
    import io
    import logging
    import telemetry
    logger = telemetry.get_logger("reality_check.web")
    logger.setLevel(logging.INFO)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logger.handlers[0].formatter)
    logger.addHandler(handler)
    try:
        class _Ctx:
            aws_request_id = "aws-req-UNITTEST"
        web.lambda_handler(_ev("POST", "/api/run", {"contract": "northstar"}), _Ctx())
    finally:
        logger.removeHandler(handler)
    rids = set()
    for line in buf.getvalue().splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if "request_id" in o:
                rids.add(o["request_id"])
    # The web logger's request + response lines must both carry the one id.
    assert rids == {"aws-req-UNITTEST"}, rids


def _all_tests():
    g = globals()
    return [g[n] for n in sorted(g) if n.startswith("test_") and callable(g[n])]


if __name__ == "__main__":
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    passed = failed = 0
    for fn in _all_tests():
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
