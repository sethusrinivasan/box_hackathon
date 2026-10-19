"""
billing.py — multi-source billing + credit harvest for the cost tracker.

The default Cost Explorer UnblendedCost nets usage against credits and collapses
to ~$0 on a credit-covered account, which hides the real picture. This module
pulls the FULL picture from several AWS billing sources so the tracker can show
both "what you pay today" (≈$0 under credits) and "what it would cost once
credits are gone" (gross usage) — and project how long the credits last.

Sources harvested:
  * Cost Explorer, split by RECORD_TYPE:
        Usage  -> gross, pre-credit spend  (the real consumption)
        Credit -> credits applied (negative)
        and the plain net UnblendedCost / NetUnblendedCost.
  * freetier:GetAccountPlanState -> plan type, credits REMAINING, expiry date.
  * freetier:GetFreeTierUsage    -> classic free-tier limit usage, if any.

Credit accounting the tracker records:
  granted (if derivable), remaining, consumed-to-date, daily gross burn, and a
  runway projection whose BINDING limit is whichever comes first: credits
  running out, or credits expiring.
"""
from __future__ import annotations

import datetime as dt
import logging
import os

log = logging.getLogger("cost_collector.billing")

REGION = os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_REGION", "us-east-1")

# The new-experience FREE plan grants $200 in promotional credits. Used only to
# estimate "granted" when remaining is known; override via env if your grant
# differs. If unset/unknown we report granted as null rather than guessing.
ASSUMED_CREDIT_GRANT = float(os.environ.get("ASSUMED_CREDIT_GRANT_USD", "200") or 0)

# boto3 clients are created lazily so pure helpers (e.g. credit_projection) can
# be imported and unit-tested without boto3 or AWS credentials present.
_clients: dict = {}


def _ce_client():
    if "ce" not in _clients:
        import boto3
        _clients["ce"] = boto3.client("ce", region_name=REGION)
    return _clients["ce"]


def _ft_client():
    if "ft" not in _clients:
        import boto3
        _clients["ft"] = boto3.client("freetier", region_name="us-east-1")  # us-east-1 only
    return _clients["ft"]


def _ce_total(start: str, end: str, record_type: str | None = None,
              metric: str = "UnblendedCost") -> float:
    kwargs = dict(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY" if _span_days(start, end) > 1 else "DAILY",
        Metrics=[metric],
    )
    if record_type:
        kwargs["Filter"] = {"Dimensions": {"Key": "RECORD_TYPE",
                                           "Values": [record_type]}}
    res = _ce_client().get_cost_and_usage(**kwargs)
    total = 0.0
    for period in res["ResultsByTime"]:
        amt = period["Total"].get(metric, {}).get("Amount")
        if amt is not None:
            total += float(amt)
    return total


def _span_days(start: str, end: str) -> int:
    return (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days


def account_plan() -> dict:
    """Plan type, remaining credits, and expiry (freetier:GetAccountPlanState)."""
    out = {"plan_type": None, "credits_remaining": None,
           "credits_expiry": None, "credits_granted": None}
    try:
        r = _ft_client().get_account_plan_state()
        out["plan_type"] = r.get("accountPlanType")
        rem = (r.get("accountPlanRemainingCredits") or {}).get("amount")
        out["credits_remaining"] = float(rem) if rem is not None else None
        exp = r.get("accountPlanExpirationDate")
        out["credits_expiry"] = exp.isoformat() if isinstance(exp, dt.datetime) else exp
        if out["credits_remaining"] is not None and ASSUMED_CREDIT_GRANT > 0:
            out["credits_granted"] = ASSUMED_CREDIT_GRANT
    except Exception as exc:  # plan API may be unavailable on some accounts
        log.warning("get_account_plan_state failed: %s", exc)
    return out


def credit_projection(credits_remaining: float | None,
                      credits_expiry: str | None,
                      avg_daily_gross: float) -> dict:
    """Project credit runway. The binding limit is whichever is sooner:
    credits exhausted at the current burn, or credits expiring."""
    today = dt.datetime.now(dt.timezone.utc).date()
    proj: dict = {
        "avg_daily_gross_usd": round(avg_daily_gross, 6),
        "days_until_exhaustion": None,
        "projected_exhaustion_date": None,
        "days_until_expiry": None,
        "binding_limit": None,
        "credit_waste_if_expiry_binds_usd": None,
    }
    if credits_expiry:
        try:
            exp_date = dt.date.fromisoformat(str(credits_expiry)[:10])
            proj["days_until_expiry"] = (exp_date - today).days
        except ValueError:
            exp_date = None
    else:
        exp_date = None

    if credits_remaining is not None and avg_daily_gross > 0:
        days = credits_remaining / avg_daily_gross
        proj["days_until_exhaustion"] = round(days, 1)
        proj["projected_exhaustion_date"] = (
            today + dt.timedelta(days=int(min(days, 36500)))).isoformat()

    exh = proj["days_until_exhaustion"]
    exp = proj["days_until_expiry"]
    if exh is not None and exp is not None:
        if exh <= exp:
            proj["binding_limit"] = "exhaustion"
        else:
            proj["binding_limit"] = "expiry"
            # credits that would expire unused at this burn
            if credits_remaining is not None:
                used_by_expiry = avg_daily_gross * exp
                proj["credit_waste_if_expiry_binds_usd"] = round(
                    max(credits_remaining - used_by_expiry, 0.0), 2)
    elif exh is not None:
        proj["binding_limit"] = "exhaustion"
    elif exp is not None:
        proj["binding_limit"] = "expiry"
    return proj


def free_tier_usage() -> list[dict]:
    """Classic free-tier limit usage (empty on credit-only accounts)."""
    rows: list[dict] = []
    try:
        paginator = _ft_client().get_paginator("get_free_tier_usage")
        for page in paginator.paginate():
            for u in page.get("freeTierUsages", []):
                rows.append({
                    "service": u.get("service"),
                    "operation": u.get("operation"),
                    "usage_type": u.get("usageType"),
                    "actual": u.get("actualUsageAmount"),
                    "limit": u.get("limit"),
                    "unit": u.get("unit"),
                })
    except Exception as exc:
        log.warning("get_free_tier_usage failed: %s", exc)
    return rows


def harvest(day_start: str, day_end: str, avg_daily_gross: float) -> dict:
    """Pull every billing source for a day and compute the credit projection.

    avg_daily_gross is passed in by the caller (computed from the stored
    history) so this module needs no DB access.
    """
    gross = _ce_total(day_start, day_end, record_type="Usage")
    credit = _ce_total(day_start, day_end, record_type="Credit")
    net = _ce_total(day_start, day_end)  # usage + credits combined
    net_unblended = _ce_total(day_start, day_end, metric="NetUnblendedCost")

    plan = account_plan()
    projection = credit_projection(
        plan["credits_remaining"], plan["credits_expiry"], avg_daily_gross)

    # consumed-to-date, if we can estimate a grant
    consumed = None
    if plan["credits_granted"] and plan["credits_remaining"] is not None:
        consumed = round(plan["credits_granted"] - plan["credits_remaining"], 2)

    return {
        "day_gross_usd": round(gross, 6),
        "day_credit_usd": round(credit, 6),
        "day_net_usd": round(net, 6),
        "day_net_unblended_usd": round(net_unblended, 6),
        "plan_type": plan["plan_type"],
        "credits_granted_usd": plan["credits_granted"],
        "credits_remaining_usd": plan["credits_remaining"],
        "credits_consumed_usd": consumed,
        "credits_expiry": plan["credits_expiry"],
        "projection": projection,
        "free_tier_usage": free_tier_usage(),
    }
