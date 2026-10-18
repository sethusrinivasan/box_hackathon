# Known Issues

Tracked limitations and the workarounds currently in place.

---

## KI-1 — Aurora PostgreSQL express configuration not supported in CloudFormation / CDK

**Status:** Open · [GitHub issue #3](https://github.com/sethusrinivasan/box_hackathon/issues/3) · AWS feature request drafted
**Area:** `agent/infra/cdk/reality_check_infra/cost_tracker_stack.py`
**Reported to AWS:** see [`docs/aws-feature-request-express-config-cfn.md`](./aws-feature-request-express-config-cfn.md)

### Issue

Aurora PostgreSQL "express configuration" is available via the AWS Console, CLI,
and the `CreateDBCluster` API (`WithExpressConfiguration=true`), but there is **no
corresponding property on `AWS::RDS::DBCluster`**. Because the AWS CDK
`rds.DatabaseCluster` L2 construct synthesizes `AWS::RDS::DBCluster`, CDK cannot
create an express-configuration cluster either.

Express configuration is the requested provisioning mode for the cost-tracker
database because it gives us exactly what we want with minimal config:
IAM database authentication by default, an Aurora-managed internet access gateway
(so **no NAT gateway**), a single serverless writer (no reader redundancy),
and Free Tier eligibility.

The standard CDK `DatabaseCluster` path instead forces VPC/subnets/security
groups, and optionally a customer-managed KMS key and pinned engine version —
none of which apply to (or are allowed by) express configuration.

### Workaround in place

We provision the express cluster through a **CloudFormation custom resource**
(`AwsCustomResource`) that calls:

- `rds:CreateDBCluster` with `WithExpressConfiguration=true` on create,
- `rds:DeleteDBCluster` (skipping the final snapshot) on delete.

The custom resource returns the cluster's `DbiResourceId` and endpoint, which we
feed into the collector Lambda's `rds-db:connect` IAM policy ARN and connection
env vars. Everything else (collector Lambda, daily EventBridge schedule, IAM) is
normal CDK.

Trade-offs accepted with this workaround:
- Custom-resource lifecycle complexity (update/rollback handling).
- Service-owned KMS key only (express config does not allow a CMK).
- Region default engine version (no version pin at creation).

### Exit criteria

Replace the custom resource with a native property once AWS ships
CloudFormation support for `WithExpressConfiguration` (and, ideally, CDK L2
support). See the linked GitHub issue for the AWS tracking details.

---

## KI-2 — Aurora Serverless v2 cold-resume latency on the cost views

**Status:** Open (platform-dependent) · instrumented on the CloudWatch dashboard
**Area:** `agent/cost_lambda/web.py`, `agent/cost_lambda/handler.py`

### Issue

Aurora Serverless v2 with min 0 ACU auto-pauses after idle (frugal: $0 compute
when idle). The first connection after a pause triggers a resume that can take
up to ~15s. On the public "Cloud cost" web view this showed up as occasional
cold-load latency (and, before the fix, a 500 when the connect timeout was
exceeded).

### Workaround in place

Both DB `_connect()` paths retry once with a fresh IAM token to ride through the
resume instead of failing. We also **instrument** the behavior: the cost Lambdas
emit `DbConnectLatencyMs`, `DbColdResume`, and `DbConnectFailure` to the
`RealityCheckCost` CloudWatch namespace (see `agent/cost_lambda/metrics.py`), and
the observability dashboard has a "cost-infra" row (Row 5) charting them.

### Why we track it

This lets us watch cold-resume latency trend over months and see whether AWS
improves Aurora Serverless v2 resume times in future platform updates — at which
point we may be able to drop the retry or tighten timeouts. The p99 latency
widget has a ~15s annotation for reference.
