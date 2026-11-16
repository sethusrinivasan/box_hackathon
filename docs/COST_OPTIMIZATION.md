# Cloud Cost Optimization & Disaster Recovery Architecture

This document details the cost optimization analysis, remediation strategy (Option A), and Disaster Recovery (DR) architecture implemented for the Reality Check AWS environment.

Ongoing tracking is maintained in [GitHub Issue #7](https://github.com/sethusrinivasan/box_hackathon/issues/7).

---

## 1. Executive Summary

| Metric | Prior (Baseline) | Target (Deployed) | Impact |
| :--- | :--- | :--- | :--- |
| **Gross Spend Rate** | **$1.958 / day** ($58.74 / mo) | **~$0.15 - $0.20 / day** (~$4.50 - $6.00 / mo) | **~90% - 92% reduction** |
| **RDS Aurora Compute** | $1.502 / day (24h continuous 0.5 ACU) | ~$0.05 - $0.07 / day (0.5 ACU ~30-45m/day) | ~$1.44 / day saved |
| **CloudWatch Metrics** | $0.323 / day (38 custom metrics) | ~$0.05 / day (<10 billable metrics) | ~$0.27 / day saved |
| **Cost Explorer API** | $0.120 / day (12 queries/day) | $0.010 - $0.020 / day (1-2 queries/day) | ~$0.10 / day saved |
| **Promotional Credits** | $152.39 remaining (~84 days runway) | > 750 days runway | Extends past credit expiry |

---

## 2. Root Cause Analysis

1. **Aurora Serverless v2 Auto-Pause Invalidation (86% of total spend):**
   - The cluster was configured with `MinCapacity: 0.0` and `SecondsUntilAutoPause: 300`.
   - The CDC sync worker (`agent/cdc/sync_worker.py`) was triggered by EventBridge every 5 minutes (`rate(5 minutes)` = 288 times/day).
   - Each CDC execution established a PostgreSQL connection to query replication slots, resetting the 300-second idle timer and forcing Aurora to run continuously at 0.5 ACU ($0.06/hr).

2. **CloudWatch Custom Metric Multiplication (16.5% of spend):**
   - 38 custom EMF metrics were active across `RealityCheck`, `RealityCheckCost`, and `RealityCheckCDC`.
   - Adding dimensions such as `fn="collector"` and `fn="web"` multiplied metric series beyond the 10 free metrics tier ($0.30/metric/month).

3. **Cost Explorer API Redundancy (6.1% of spend):**
   - `billing.harvest()` performed 4 separate queries (`Usage`, `Credit`, `UnblendedCost`, `NetUnblendedCost`), and `handler.py` ran 2 tag/service groupings twice daily (12 calls/day at $0.01/call).

---

## 3. Implemented Remediations (Option A)

### A. Aurora Auto-Pause Unblocked & CDC Rescheduled
- Rescheduled CDC Sync from `rate(5 minutes)` to `cron(30 6 * * ? *)` in `agent/infra/cdk/reality_check_infra/cdc_stack.py`.
- Scheduled immediately following daily cost collection (`cron(0 6 * * ? *)`).
- Allows Aurora Serverless v2 to auto-pause after 5 idle minutes and remain at 0.0 ACU ($0.00 compute) ~23.5 hours every day.

### B. Cost Explorer Query Consolidation
- In `agent/cost_lambda/billing.py`, consolidated `UnblendedCost` and `NetUnblendedCost` into a single grouped query.
- Reused service-level grouping sums rather than issuing duplicate unblended total calls.
- Reduced collector frequency from twice daily to once daily (`cron(0 6 * * ? *)`). Total API queries dropped from 12 to 1-2 per day.

### C. Metric Cardinality Reduction & Alarm Alignment
- Removed `fn` dimensions from `DbConnectLatencyMs`, `DbColdResume`, and `DbConnectFailure` in `agent/cost_lambda/handler.py` and `web.py`.
- Aligned `RealityCheck-CdcReplicationLag` CloudWatch alarm threshold from 900s (15 min) to 86,400s (24h) with 1h evaluation period to support daily sync cadence.

### D. Change Tracking & Hyperlink Attribution
- Extended `daily_cost` schema with `change_details` (TEXT) and `change_url` (TEXT).
- Added `Changes / Deployments` column to the Daily history table in `agent/cost_lambda/web.py` with direct links to GitHub commits.
- Backfilled architectural milestones for full visibility into system evolution.

---

## 4. Disaster Recovery (DR) Architecture

To safeguard against catastrophic data loss in AWS, a complete Disaster Recovery toolset resides in `dr/`:

- **Compact Cold Storage:** Active tables (`daily_cost`, `resource_inventory`, `__cdc_metadata`) are exported to ZSTD-compressed Apache Parquet files committed to Git.
- **Local Restoration:** `dr/docker-compose.yml` runs a standalone PostgreSQL 16 container with `pgvector` on port 5433.
- **Automated Verification:** `dr/verify_dr.sh` spins up the container, runs `dr/restore_parquet_to_postgres.py`, verifies row counts and financial totals ($46.2690 gross), and tears down the container.
- **Zero-Cloud Dependency:** Database state can be inspected or recovered entirely offline.
