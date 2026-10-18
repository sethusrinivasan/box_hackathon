# AWS Feature Request: CloudFormation / CDK support for Aurora PostgreSQL "express configuration"

> Ready-to-submit write-up. The AWS Support **API** is unavailable on this account
> (Premium Support subscription required — `SubscriptionRequiredException` on
> `support:DescribeSeverityLevels`), so submit this through the **AWS Console**:
>
> - **Feature request (any support tier):** [AWS re:Post](https://repost.aws/) →
>   post in the *Amazon RDS / Aurora* topic, or use the **"Request a feature"**
>   option in the console service feedback widget.
> - **Support case (Business/Enterprise only):** Console → **Support** →
>   **Create case** → *Technical* → *Amazon Aurora* → *PostgreSQL*.
>
> Copy the sections below into the case/post body.

---

## Title

Add CloudFormation / CDK support for Aurora PostgreSQL express configuration (`WithExpressConfiguration`)

## Category

Amazon Aurora → PostgreSQL → Provisioning / Infrastructure as Code

## Summary

Aurora PostgreSQL "express configuration" is available via the Console, AWS CLI,
and the `CreateDBCluster` API (parameter `WithExpressConfiguration=true`), but it
is **not available through AWS CloudFormation** (and therefore not through the AWS
CDK `rds.DatabaseCluster` L2 construct, which synthesizes CloudFormation). This
forces infrastructure-as-code users to either (a) provision the cluster
out-of-band with the CLI, breaking a single reproducible deploy, or (b) wrap the
raw `CreateDBCluster` API in a CloudFormation custom resource, which is
error-prone around update/delete/rollback. Please add a first-class property to
`AWS::RDS::DBCluster` to opt into express configuration.

## Environment / exact details

- **Account:** 596090776030
- **Region:** us-east-2 (Ohio) — a supported region for express configuration
- **AWS CLI:** aws-cli/2.38.1 (parameter present: `CreateDBCluster.WithExpressConfiguration = true`, verified via botocore service model)
- **AWS CDK:** 2.1145.0
- **Engine:** aurora-postgresql (express config uses the region default engine version)

## What works today

Express configuration works via the CLI/API in one call, provisioning the
cluster, a single serverless instance (writer), the managed internet access
gateway, and IAM database authentication for the admin user:

```bash
aws rds create-db-cluster \
  --db-cluster-identifier sample-express-cluster \
  --engine aurora-postgresql \
  --with-express-configuration
```

## The gap

- `AWS::RDS::DBCluster` has **no property** corresponding to
  `WithExpressConfiguration`. CloudFormation cannot create an express-config
  cluster.
- Because CDK's `rds.DatabaseCluster` emits `AWS::RDS::DBCluster`, the CDK L2
  construct cannot create one either. The L2 instead requires VPC, subnets,
  security groups, and (optionally) a customer-managed KMS key and a pinned
  engine version — all of which are **not applicable** to express configuration
  (express clusters are not VPC-associated, use a service-owned key, and take the
  region default engine version).
- Net effect: an IaC user who wants the express model must drop out of
  CloudFormation/CDK for the database, or build a custom resource around the raw
  API. Both undermine the "ready in seconds, minimal configuration" value
  proposition of the feature for teams who standardize on IaC.

## Requested change

1. Add a boolean property to `AWS::RDS::DBCluster`, e.g.
   `WithExpressConfiguration`, that maps to the `CreateDBCluster` API parameter.
2. When set, allow the VPC/subnet/security-group/KMS/engine-version properties to
   be **omitted** (CloudFormation should not require them, mirroring the API).
3. Surface the resulting cluster attributes needed by downstream resources —
   especially the **DbiResourceId** (used in `rds-db:connect` IAM policy ARNs) and
   the **cluster endpoint** — as `Fn::GetAtt` return values so IAM-auth clients
   can be wired up in the same template.
4. Follow-on: expose this via the CDK `rds` module (either a flag on
   `DatabaseCluster` or a dedicated `ExpressCluster` construct) once the
   CloudFormation property ships.

## Business impact / use case

We run a frugal, serverless-first workload (daily cost-tracking collector Lambda
writing to Aurora Serverless v2 PostgreSQL via **IAM database authentication only,
native PostgreSQL protocol, no stored secrets**). Express configuration is an
ideal fit (IAM-auth by default, internet access gateway so no NAT gateway is
needed, single writer, Free Tier eligible). The only blocker to adopting it
cleanly is the absence of CloudFormation/CDK support, which prevents a
single-command, reproducible `cdk deploy`.

## Workaround in use

We provision the express cluster via a CloudFormation custom resource that calls
`CreateDBCluster` with `WithExpressConfiguration=true` (and `DeleteDBCluster` on
stack delete), then reference the cluster's DbiResourceId/endpoint for the
collector Lambda's IAM-auth policy. This works but adds custom-resource lifecycle
complexity we would prefer to avoid.

## References

- Create with express configuration (Aurora User Guide): https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/CHAP_GettingStartedAurora.AuroraPostgreSQL.ExpressConfig.html
- Launch announcement (March 2026): https://aws.amazon.com/about-aws/whats-new/2026/03/amazon-aurora-postgresql-database/
- `CreateDBCluster` API reference (`WithExpressConfiguration`): https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_CreateDBCluster.html
