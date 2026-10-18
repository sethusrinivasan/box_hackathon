"""
CostTrackerStack — daily holistic cost tracking into Aurora Serverless v2 Postgres.

Frugal, foundational-services-only, express-configuration design.

Aurora provisioning (see docs/KNOWN_ISSUES.md KI-1 and GitHub issue #3):
  Aurora PostgreSQL "express configuration" has no CloudFormation property, so we
  create the cluster with an AwsCustomResource that calls the CreateDBCluster API
  with WithExpressConfiguration=true (and DeleteDBCluster on teardown). Express
  config gives us exactly the frugal posture we want:
    * IAM database authentication by default (no password, no Secrets Manager).
    * An Aurora-managed internet access gateway → reachable over the internet
      with NO NAT gateway and NO customer VPC.
    * A SINGLE serverless writer (no reader redundancy) — lowest tier.
    * Service-owned KMS encryption; region-default engine version.

Collector Lambda:
  * arm64 (Graviton), 256 MB, OUTSIDE any VPC — so it reaches both the Cost
    Explorer API (public, no PrivateLink) and the Aurora internet access gateway
    without a NAT gateway.
  * NATIVE PostgreSQL wire protocol via psycopg. NO RDS Data API.
  * IAM DATABASE AUTHENTICATION only: mints a short-lived IAM auth token at
    runtime and uses it as the password. Nothing persisted anywhere.
  * Connects as a dedicated least-privilege `cost_collector` role (created via
    agent/cost_lambda/schema.sql, GRANT rds_iam), NOT the admin user.

  * EventBridge Scheduler invokes the collector once per day.
"""
from __future__ import annotations

import os

import jsii
from aws_cdk import (
    Duration,
    Stack,
    CfnOutput,
    RemovalPolicy,
    BundlingOptions,
    DockerImage,
    ILocalBundling,
    aws_lambda as lambda_,
    aws_iam as iam,
    aws_logs as logs,
    aws_scheduler as scheduler,
    custom_resources as cr,
)
from constructs import Construct

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)

DB_NAME = "postgres"            # express config default database
DB_IAM_USER = "cost_collector"  # dedicated least-privilege IAM-auth role (read/write)
DB_READER_USER = "cost_reader"  # read-only IAM-auth role for the public web view
DB_PORT = 5432


class CostTrackerStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cluster_id: str = "reality-check-cost-tracker",
        db_name: str = DB_NAME,
        db_iam_user: str = DB_IAM_USER,
        db_reader_user: str = DB_READER_USER,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Aurora express-configuration cluster (via provider Lambda) -----
        # WORKAROUND for KI-1 / issue #3: no CFN support for express config.
        # A dedicated provider Lambda handles the full lifecycle robustly:
        # create → wait for endpoint; delete → drop instance then cluster.
        provider_fn = lambda_.Function(
            self, "ExpressClusterProvider",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="express_cluster_provider.handler",
            code=lambda_.Code.from_asset(
                os.path.join(os.path.dirname(__file__)),
                exclude=["**", "!express_cluster_provider.py"],
            ),
            timeout=Duration.minutes(12),
            memory_size=256,
            description="Custom-resource provider for Aurora express-config cluster.",
        )
        provider_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "rds:CreateDBCluster",
                "rds:DeleteDBCluster",
                "rds:DescribeDBClusters",
                "rds:CreateDBInstance",       # express config creates the writer
                "rds:DeleteDBInstance",
                "rds:DescribeDBInstances",
                "rds:EnableInternetAccessGateway",
                "rds:AddTagsToResource",
            ],
            resources=["*"],
        ))
        provider_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["iam:CreateServiceLinkedRole"], resources=["*"],
        ))
        provider_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ec2:DescribeAvailabilityZones"], resources=["*"],
        ))

        provider = cr.Provider(
            self, "ExpressClusterProviderFramework",
            on_event_handler=provider_fn,
            log_group=logs.LogGroup(
                self, "ExpressProviderLogGroup",
                retention=logs.RetentionDays.THREE_DAYS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        from aws_cdk import CustomResource
        cluster = CustomResource(
            self, "ExpressCluster",
            service_token=provider.service_token,
            properties={"ClusterId": cluster_id},
        )

        db_resource_id = cluster.get_att_string("DbClusterResourceId")
        db_endpoint = cluster.get_att_string("Endpoint")

        # --- Collector Lambda (OUTSIDE the VPC; no NAT needed) ---------------
        log_group = logs.LogGroup(
            self, "CostCollectorLogGroup",
            retention=logs.RetentionDays.THREE_DAYS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        collector = lambda_.Function(
            self, "CostCollectorFunction",
            function_name="reality-check-cost-collector",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(
                os.path.join(_REPO_ROOT, "agent", "cost_lambda"),
                bundling=self._bundling(),
            ),
            timeout=Duration.seconds(120),
            memory_size=256,
            log_group=log_group,
            environment={
                "DB_HOST": db_endpoint,
                "DB_PORT": str(DB_PORT),
                "DB_NAME": db_name,
                "DB_IAM_USER": db_iam_user,
                "AWS_REGION_NAME": self.region,
                "LOG_LEVEL": "INFO",
            },
            description="Daily holistic AWS cost collector → Aurora (IAM-auth, native PG).",
        )

        # IAM db auth: connect ONLY as the dedicated db user on THIS cluster.
        # ARN: arn:aws:rds-db:region:acct:dbuser:<DbiResourceId>/<user>
        collector.add_to_role_policy(iam.PolicyStatement(
            actions=["rds-db:connect"],
            resources=[
                f"arn:aws:rds-db:{self.region}:{self.account}:dbuser:"
                f"{db_resource_id}/{db_iam_user}"
            ],
        ))
        # Cost Explorer read (holistic cost). CE actions are not resource-scopable.
        collector.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "ce:GetCostAndUsage",
                "ce:GetCostForecast",
                "ce:GetDimensionValues",
            ],
            resources=["*"],
        ))

        # --- Public "Cloud cost" web view (separate Lambda + Function URL) --
        # Its own function so the offline contract-review demo keeps its
        # zero-privilege posture. Read-only: connects as `cost_reader` (IAM auth,
        # SELECT-only on daily_cost). No Cost Explorer access — it only reads the
        # rolled-up history the collector already stored.
        web_log_group = logs.LogGroup(
            self, "CostWebLogGroup",
            retention=logs.RetentionDays.THREE_DAYS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        cost_web = lambda_.Function(
            self, "CostWebFunction",
            function_name="reality-check-cost-web",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="web.lambda_handler",
            code=lambda_.Code.from_asset(
                os.path.join(_REPO_ROOT, "agent", "cost_lambda"),
                bundling=self._bundling(),
            ),
            timeout=Duration.seconds(30),
            memory_size=256,
            log_group=web_log_group,
            reserved_concurrent_executions=5,   # cap abuse cost on a public URL
            environment={
                "DB_HOST": db_endpoint,
                "DB_PORT": str(DB_PORT),
                "DB_NAME": db_name,
                "DB_READER_USER": db_reader_user,
                "AWS_REGION_NAME": self.region,
                "COST_WINDOW_DAYS": "30",
            },
            description="Public Cloud-cost web view (read-only, IAM-auth, native PG).",
        )
        cost_web.add_to_role_policy(iam.PolicyStatement(
            actions=["rds-db:connect"],
            resources=[
                f"arn:aws:rds-db:{self.region}:{self.account}:dbuser:"
                f"{db_resource_id}/{db_reader_user}"
            ],
        ))
        cost_web_url = cost_web.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE,
            cors=lambda_.FunctionUrlCorsOptions(
                allowed_origins=["*"],
                allowed_methods=[lambda_.HttpMethod.GET],
            ),
        )

        # --- Daily schedule (EventBridge Scheduler) -------------------------
        sched_role = iam.Role(
            self, "CostScheduleRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
            description="Lets EventBridge Scheduler invoke the cost collector.",
        )
        collector.grant_invoke(sched_role)

        scheduler.CfnSchedule(
            self, "DailyCostSchedule",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                mode="FLEXIBLE", maximum_window_in_minutes=60,
            ),
            # 06:00 UTC daily — after the prior UTC day has fully closed in CE.
            schedule_expression="cron(0 6 * * ? *)",
            target=scheduler.CfnSchedule.TargetProperty(
                arn=collector.function_arn,
                role_arn=sched_role.role_arn,
                input='{"source":"daily-schedule"}',
            ),
            description="Daily Reality Check holistic cost collection.",
            state="ENABLED",
        )

        # --- Outputs ---------------------------------------------------------
        CfnOutput(self, "DbEndpoint", value=db_endpoint,
                  description="Aurora cluster endpoint (connect with IAM-auth token).")
        CfnOutput(self, "DbResourceId", value=db_resource_id,
                  description="DbiResourceId used in the rds-db:connect ARN.")
        CfnOutput(self, "DbName", value=db_name)
        CfnOutput(self, "DbIamUser", value=db_iam_user,
                  description="Least-privilege IAM-auth DB login (create via schema.sql).")
        CfnOutput(self, "CollectorFunctionName", value=collector.function_name)
        CfnOutput(self, "CostWebUrl", value=cost_web_url.url,
                  description="Public Cloud-cost web view URL.")
        CfnOutput(self, "DbReaderUser", value=db_reader_user,
                  description="Read-only IAM-auth DB login for the web view.")
        CfnOutput(self, "BootstrapHint",
                  value=("Run agent/cost_lambda/schema.sql once as the express admin "
                         "user to create the cost_collector IAM role."),
                  description="One-time SQL bootstrap reminder.")

    @staticmethod
    def _bundling() -> BundlingOptions:
        """Package the collector + its deps (psycopg binary) for arm64.

        Prefer LOCAL bundling: pip can fetch prebuilt manylinux aarch64 wheels on
        any host via --platform, so no Docker is needed. Docker (arm64 Lambda
        image) is kept as the CDK-required fallback."""
        # Docker fallback command.
        docker_cmd = " && ".join([
            "cp -r /asset-input/. /asset-output/",
            "pip install -r /asset-output/requirements.txt -t /asset-output "
            "--platform manylinux2014_aarch64 --implementation cp "
            "--python-version 3.12 --only-binary=:all: --upgrade",
            "find /asset-output -name __pycache__ -type d -prune -exec rm -rf {} + || true",
        ])
        return BundlingOptions(
            image=DockerImage.from_registry("public.ecr.aws/lambda/python:3.12-arm64"),
            command=["bash", "-c", docker_cmd],
            local=_LocalCollectorBundler(),
        )


@jsii.implements(ILocalBundling)
class _LocalCollectorBundler:
    """Host-side packaging: copy sources, then pip-install arm64 wheels with
    --platform so psycopg[binary] resolves to manylinux aarch64 regardless of
    the host architecture. Returns False (→ Docker) if pip can't resolve."""

    def try_bundle(self, output_dir: str, options) -> bool:  # noqa: ANN001
        import shutil
        import subprocess
        src = os.path.join(_REPO_ROOT, "agent", "cost_lambda")
        try:
            # 1) copy the collector sources
            for name in os.listdir(src):
                s = os.path.join(src, name)
                d = os.path.join(output_dir, name)
                if os.path.isdir(s):
                    shutil.copytree(s, d, dirs_exist_ok=True)
                else:
                    shutil.copy2(s, d)
            # 2) install arm64 deps into the package dir
            subprocess.run(
                [
                    "pip", "install",
                    "-r", os.path.join(output_dir, "requirements.txt"),
                    "-t", output_dir,
                    "--platform", "manylinux2014_aarch64",
                    "--implementation", "cp",
                    "--python-version", "3.12",
                    "--only-binary=:all:",
                    "--upgrade",
                ],
                check=True,
            )
            # 3) drop caches
            subprocess.run(
                ["bash", "-c",
                 f"find '{output_dir}' -name __pycache__ -type d -prune "
                 "-exec rm -rf {} + || true"],
                check=False,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            return False
