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
