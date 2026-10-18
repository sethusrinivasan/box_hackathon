"""
test_canary.py — unit tests for the hourly web-demo canary (agent/canary.py).

Exercises run_canary() against a stubbed HTTP layer so no network is needed:
verifies it passes when every endpoint behaves, fails when one misbehaves, and
emits the expected CanarySuccess/CanaryFailure/CanaryCheck metrics.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
for p in (HERE, os.path.join(ROOT, "box")):
    sys.path.insert(0, p)

os.environ["DEMO_URL"] = "https://demo.example"
import canary  # noqa: E402
import telemetry  # noqa: E402


# --- a fake _request that returns scripted (status, body) per path ---------
def _good(method, path, body=None):
    if path == "/healthz":
        return 200, b'{"ok": true, "mode": "offline"}', 10.0
    if path == "/":
        return 200, b"<html>Reality Check live demo</html>", 12.0
    if path == "/api/run":
        return 200, b'{"result": {"summary": {"Risk identified": 3}}}', 20.0
    if path.startswith("/report.pdf"):
        return 200, b"%PDF-1.4 ...", 15.0
    return 404, b"", 5.0


def _broken_review(method, path, body=None):
    if path == "/api/run":
        return 500, b'{"error": "boom"}', 20.0
    return _good(method, path, body)


def test_canary_all_pass(monkeypatch=None):
    orig = canary._request
    canary._request = _good
    try:
        with telemetry.capture_metrics() as cap:
            out = canary.run_canary()
        assert out["ok"] is True
        assert all(c["ok"] for c in out["checks"])
        assert cap.count("CanarySuccess") == 1
        assert cap.total("CanarySuccess") == 1.0
        assert cap.total("CanaryFailure") == 0.0
        assert cap.count("CanaryCheck", result="pass") == 4
        assert cap.count("CanaryCheck", result="fail") == 0
    finally:
        canary._request = orig


def test_canary_detects_broken_endpoint():
    orig = canary._request
    canary._request = _broken_review
    try:
        with telemetry.capture_metrics() as cap:
            out = canary.run_canary()
        assert out["ok"] is False
        failed = [c["endpoint"] for c in out["checks"] if not c["ok"]]
        assert failed == ["review"]
        assert cap.total("CanarySuccess") == 0.0
        assert cap.total("CanaryFailure") == 1.0
        assert cap.count("CanaryCheck", result="fail") == 1
    finally:
        canary._request = orig


def test_lambda_handler_raises_on_failure():
    orig = canary._request
    canary._request = _broken_review
    try:
        raised = False
        try:
            canary.lambda_handler({}, None)
        except RuntimeError:
            raised = True
        assert raised, "handler must raise on a failed canary so Lambda Errors fire"
    finally:
        canary._request = orig


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
