"""
Offline unit tests for the credit-runway projection logic in billing.py.

Pure-function tests — no AWS, no DB. Run: python3 agent/cost_lambda/test_billing.py
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import billing  # noqa: E402

_passed = _failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"PASS {name}")
    else:
        _failed += 1
        print(f"FAIL {name}")


def _future(days):
    return (dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=days)).isoformat()


# 1) Expiry binds when credits would outlast the grant window at current burn.
#    $139.51 remaining, tiny $0.03/day burn, expiry 180 days out → expiry binds,
#    and most credits would expire unused.
p = billing.credit_projection(139.51, _future(180), 0.03)
check("expiry binds when burn is tiny", p["binding_limit"] == "expiry")
check("waste computed on expiry bind", (p["credit_waste_if_expiry_binds_usd"] or 0) > 100)
check("days_until_expiry ~180", abs(p["days_until_expiry"] - 180) <= 1)

# 2) Exhaustion binds when burn is high enough to drain before expiry.
#    $100 remaining, $10/day → ~10 days, well before a 180-day expiry.
p = billing.credit_projection(100.0, _future(180), 10.0)
check("exhaustion binds when burn is high", p["binding_limit"] == "exhaustion")
check("days_until_exhaustion ~10", abs(p["days_until_exhaustion"] - 10) <= 1)

# 3) Zero burn → no exhaustion date, expiry is the only limit.
p = billing.credit_projection(50.0, _future(90), 0.0)
check("zero burn → no exhaustion", p["days_until_exhaustion"] is None)
check("zero burn → expiry binds", p["binding_limit"] == "expiry")

# 4) Unknown credits → projection degrades gracefully, no crash.
p = billing.credit_projection(None, None, 1.0)
check("unknown credits → no binding", p["binding_limit"] is None)
check("avg burn echoed back", p["avg_daily_gross_usd"] == 1.0)


if __name__ == "__main__":
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
