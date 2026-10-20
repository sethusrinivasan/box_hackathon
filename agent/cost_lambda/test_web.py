"""
test_web.py — unit tests for the Cloud Cost web portal and Aurora latency breakdown.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ.setdefault("DB_HOST", "mock-aurora.cluster-test.us-east-2.rds.amazonaws.com")
os.environ.setdefault("AWS_REGION_NAME", "us-east-2")

# Mock boto3 and psycopg if not installed in the local test environment
mock_boto3 = MagicMock()
mock_psycopg = MagicMock()
mock_certifi = MagicMock()
sys.modules.setdefault("boto3", mock_boto3)
sys.modules.setdefault("psycopg", mock_psycopg)
sys.modules.setdefault("certifi", mock_certifi)

from metrics import LatencyTracker
import web


def test_latency_tracker_summary():
    tracker = LatencyTracker()
    tracker.tls_version = "TLSv1.3"
    tracker.tls_cipher = "TLS_AES_256_GCM_SHA384"
    tracker.record("AWS RDS", "rds:generate_db_auth_token", 10.0)
    tracker.record("Amazon Aurora PostgreSQL", "psycopg:connect (TLS + IAM auth)", 90.0)
    tracker.record("Amazon Aurora PostgreSQL", "Query daily_cost (30 days)", 20.0)

    summary = tracker.summary()
    assert summary["total_calls"] == 3
    assert summary["total_ms"] == 120.0
    assert summary["cold_resume"] is False
    assert summary["tls_version"] == "TLSv1.3"
    assert summary["tls_cipher"] == "TLS_AES_256_GCM_SHA384"
    assert "Warm" in summary["database_status"]
    assert len(summary["services"]) == 2

    aurora = next(s for s in summary["services"] if s["service"] == "Amazon Aurora PostgreSQL")
    assert aurora["calls"] == 2
    assert aurora["total_ms"] == 110.0
    assert aurora["percent"] == round(110.0 / 120.0 * 100, 1)
    assert len(aurora["sub_operations"]) == 2
    assert aurora["sub_operations"][0]["operation"] == "psycopg:connect (TLS + IAM auth)"
    assert aurora["sub_operations"][0]["total_ms"] == 90.0
    assert aurora["sub_operations"][1]["operation"] == "Query daily_cost (30 days)"
    assert aurora["sub_operations"][1]["total_ms"] == 20.0


def test_latency_tracker_cold_resume_flag():
    tracker = LatencyTracker()
    tracker.cold_resume = True
    tracker.record("Amazon Aurora PostgreSQL", "Aurora Serverless v2 [Compute cold resume (0 → 0.5 ACU)]", 10540.0)
    tracker.record("Amazon Aurora PostgreSQL", "psycopg:connect [Server IAM auth]", 397.3)
    tracker.record("Amazon Aurora PostgreSQL", "psycopg:connect [TLS handshake - TLSv1.3]", 65.0)
    tracker.record("Amazon Aurora PostgreSQL", "psycopg:connect [TCP connect]", 25.0)

    summary = tracker.summary()
    assert summary["cold_resume"] is True
    assert "Cold Resume" in summary["database_status"]
    aurora = next(s for s in summary["services"] if s["service"] == "Amazon Aurora PostgreSQL")
    assert len(aurora["sub_operations"]) == 4
    cold_op = next(op for op in aurora["sub_operations"] if "Compute cold resume" in op["operation"])
    assert cold_op["total_ms"] == 10540.0
    auth_op = next(op for op in aurora["sub_operations"] if "Server IAM auth" in op["operation"])
    assert auth_op["total_ms"] == 397.3


def test_render_includes_latency_breakdown():
    mock_data = {
        "window_days": 30,
        "days_recorded": 2,
        "window_gross_usd": 1.25,
        "window_net_usd": 0.0,
        "currency": "USD",
        "credit": {
            "plan_type": "FREE",
            "credits_granted_usd": 200.0,
            "credits_remaining_usd": 150.0,
            "credits_consumed_usd": 50.0,
            "credits_expiry": "2027-01-01",
            "projection": {"binding_limit": "expiry", "days_until_expiry": 100, "avg_daily_gross_usd": 1.25},
        },
        "resource_count": 15,
        "daily": [{"date": "2026-10-18", "gross_usd": 1.25, "net_usd": 0.0, "currency": "USD", "services": {}}],
        "top_services": [{"service": "Amazon Relational Database Service", "usd": 1.0}],
        "latency_breakdown": {
            "total_ms": 150.5,
            "total_calls": 3,
            "cold_resume": False,
            "tls_version": "TLSv1.3",
            "database_status": "Aurora Serverless v2 (Warm)",
            "services": [
                {
                    "service": "Amazon Aurora PostgreSQL",
                    "calls": 2,
                    "total_ms": 140.0,
                    "percent": 93.0,
                    "operations": {"psycopg:connect": 1, "Query daily_cost": 1},
                    "sub_operations": [
                        {"operation": "psycopg:connect (TLS + IAM auth)", "calls": 1, "total_ms": 120.0, "percent_of_service": 85.7},
                        {"operation": "Query daily_cost (30 days)", "calls": 1, "total_ms": 20.0, "percent_of_service": 14.3},
                    ],
                },
                {
                    "service": "AWS RDS",
                    "calls": 1,
                    "total_ms": 10.5,
                    "percent": 7.0,
                    "operations": {"rds:generate_db_auth_token": 1},
                    "sub_operations": [
                        {"operation": "rds:generate_db_auth_token", "calls": 1, "total_ms": 10.5, "percent_of_service": 100.0},
                    ],
                },
            ],
        },
    }

    html = web._render(mock_data)
    assert "AWS Service &amp; Aurora Latency Breakdown" in html
    assert "Aurora Serverless v2 (Warm) · TLSv1.3" in html
    assert "150.5 ms" in html
    assert "Amazon Aurora PostgreSQL" in html
    assert "Trend (30d)" in html
    assert "<svg" in html
    assert "↳" in html
    assert "psycopg:connect (TLS + IAM auth)" in html
    assert "120.0 ms" in html
    assert "85.7%" in html


def test_sparkline_svg_rendering():
    # Empty
    assert "class=\"sparkline\"" in web._sparkline_svg([])

    # Single point
    single = web._sparkline_svg([0.05])
    assert "<polyline" in single
    assert "<circle" in single
    assert "Latest: $0.0500" in single

    # Rising spend (amber #d29922)
    rising = web._sparkline_svg([0.01, 0.05])
    assert "#d29922" in rising

    # Declining spend (green #3fb950)
    declining = web._sparkline_svg([0.05, 0.01])
    assert "#3fb950" in declining

    # Steady / baseline zero (blue #58a6ff)
    steady = web._sparkline_svg([0.0, 0.0, 0.0])
    assert "#58a6ff" in steady


def test_fetch_costs_mocked():
    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = [
        (
            dt.date(2026, 10, 18),
            0.50,
            0.0,
            "USD",
            {"Amazon RDS": 0.50},
            "FREE",
            150.0,
            50.0,
            200.0,
            dt.date(2027, 1, 1),
            {"binding_limit": "expiry"},
        )
    ]
    mock_cur.fetchone.return_value = [10]

    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.__enter__.return_value = mock_conn

    with patch("web._connect", return_value=mock_conn):
        data = web._fetch_costs()
        assert data["window_gross_usd"] == 0.5
        assert "latency_breakdown" in data
        assert data["latency_breakdown"]["total_calls"] >= 2
        assert "gross_trend" in data
        assert data["gross_trend"] == [0.5]
        assert len(data["top_services"]) == 1
        assert data["top_services"][0]["trend"] == [0.5]


def test_web_routes():
    # /healthz
    res = web.lambda_handler({"requestContext": {"http": {"method": "GET", "path": "/healthz"}}})
    assert res["statusCode"] == 200
    assert json.loads(res["body"])["ok"] is True

    # 404
    res_404 = web.lambda_handler({"requestContext": {"http": {"method": "GET", "path": "/unknown"}}})
    assert res_404["statusCode"] == 404


if __name__ == "__main__":
    test_latency_tracker_summary()
    print("PASS test_latency_tracker_summary")
    test_latency_tracker_cold_resume_flag()
    print("PASS test_latency_tracker_cold_resume_flag")
    test_render_includes_latency_breakdown()
    print("PASS test_render_includes_latency_breakdown")
    test_sparkline_svg_rendering()
    print("PASS test_sparkline_svg_rendering")
    test_fetch_costs_mocked()
    print("PASS test_fetch_costs_mocked")
    test_web_routes()
    print("PASS test_web_routes")
    print("\n6 passed, 0 failed")
