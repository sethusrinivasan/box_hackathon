"""
cdc_stack.py — Serverless Aurora-to-DuckDB CDC Pipeline CDK Stack.

Provisions:
  1. DynamoDB Distributed Lock Table (duckdb-sync-locks) with TTL for single-writer leasing.
  2. S3 Analytical Storage Bucket (or analytical prefix) for the compact .duckdb file.
  3. Scheduled Sync Worker Lambda (singleton writer) running every 5 minutes.
  4. Stateless Reader Lambda with Function URL serving analytical queries directly
     from DuckDB with zero Aurora read-replica reliance.
  5. CloudWatch Alarms for replication lag and full re-init anomalies.
"""
from __future__ import annotations

import os

import jsii
from aws_cdk import (
    BundlingOptions,
    CfnOutput,
    DockerImage,
    Duration,
    ILocalBundling,
    RemovalPolicy,
    Stack,
    aws_cloudwatch as cw,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_scheduler as scheduler,
)
from constructs import Construct

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)


class CdcStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cluster_endpoint: str = "",
        cluster_resource_id: str = "",
        db_name: str = "postgres",
        db_iam_user: str = "cdc_sync_worker",
        db_port: int = 5432,
        sync_schedule_minutes: int = 5,
        demo_url: str = "",
        cost_web_url: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- 1. DynamoDB Distributed Lock / Lease Table --------------------
        lock_table = dynamodb.Table(
            self,
            "DuckDbSyncLockTable",
            partition_key=dynamodb.Attribute(
                name="lock_key", type=dynamodb.AttributeType.STRING
            ),
            time_to_live_attribute="ttl",
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- 2. S3 Analytical Bucket ---------------------------------------
        analytics_bucket = s3.Bucket(
            self,
            "DuckDbAnalyticsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="AbortIncompleteMultipartUploads",
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                )
            ],
        )

        # --- 3. Sync Worker Lambda (Singleton Writer) ----------------------
        worker_log_group = logs.LogGroup(
            self,
            "CdcSyncWorkerLogGroup",
            retention=logs.RetentionDays.THREE_DAYS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        worker_env = {
            "LOCK_TABLE_NAME": lock_table.table_name,
            "LOCK_KEY": "duckdb_writer_lease",
            "LEASE_DURATION_SEC": "300",
            "S3_BUCKET": analytics_bucket.bucket_name,
            "S3_KEY": "analytics/analytics.duckdb",
            "S3_STORAGE_CLASS": "STANDARD",
            "LOCAL_DB_PATH": "/tmp/analytics.duckdb",  # nosec B108
            "DB_HOST": cluster_endpoint,
            "DB_PORT": str(db_port),
            "DB_NAME": db_name,
            "DB_IAM_USER": db_iam_user,
            "AWS_REGION_NAME": self.region,
            "REPLICATION_SLOT": "duckdb_cdc_slot",
            "PUBLICATION_NAME": "duckdb_pub",
            "REPLICATED_TABLES": "daily_cost,resource_inventory",
            "EMIT_METRICS": "true",
            "CDC_METRIC_NAMESPACE": "RealityCheckCDC",
        }

        sync_worker = lambda_.Function(
            self,
            "CdcSyncWorkerFunction",
            function_name="reality-check-cdc-sync-worker",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="agent.cdc.sync_worker.lambda_handler",
            code=lambda_.Code.from_asset(
                os.path.join(_REPO_ROOT, "agent", "cdc"),
                bundling=self._bundling(),
            ),
            timeout=Duration.minutes(5),
            memory_size=512,
            reserved_concurrent_executions=1,  # Defense-in-depth concurrency lock
            log_group=worker_log_group,
            environment=worker_env,
            description="Scheduled singleton CDC worker: streams Aurora WAL → DuckDB in S3 under DynamoDB lease.",
        )

        # Grant DynamoDB lock table permissions
        lock_table.grant_read_write_data(sync_worker)

        # Grant S3 read/write permissions
        analytics_bucket.grant_read_write(sync_worker)

        # Grant RDS IAM DB connect permissions if cluster info is provided
        if cluster_resource_id:
            sync_worker.add_to_role_policy(
                iam.PolicyStatement(
                    sid="ConnectCdcDbUserOnly",
                    actions=["rds-db:connect"],
                    resources=[
                        f"arn:aws:rds-db:{self.region}:{self.account}:dbuser:"
                        f"{cluster_resource_id}/{db_iam_user}"
                    ],
                    conditions={
                        "StringEquals": {
                            "aws:ResourceAccount": self.account,
                            "aws:RequestedRegion": self.region,
                        }
                    },
                )
            )

        # --- 4. EventBridge Schedule for Sync Worker -----------------------
        sched_role = iam.Role(
            self,
            "CdcScheduleRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
            description="Allows EventBridge Scheduler to trigger the CDC Sync Worker.",
        )
        sync_worker.grant_invoke(sched_role)

        scheduler.CfnSchedule(
            self,
            "CdcSyncSchedule",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                mode="OFF"
            ),
            schedule_expression=f"rate({sync_schedule_minutes} minutes)",
            target=scheduler.CfnSchedule.TargetProperty(
                arn=sync_worker.function_arn,
                role_arn=sched_role.role_arn,
                input='{"source":"eventbridge.cdc"}',
            ),
            description=f"CDC incremental sync every {sync_schedule_minutes} minutes.",
            state="ENABLED",
        )

        # --- 5. Stateless Reader Lambda (Zero Aurora Dependency) -----------
        reader_log_group = logs.LogGroup(
            self,
            "CdcReaderLogGroup",
            retention=logs.RetentionDays.THREE_DAYS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        reader_env = {
            "S3_BUCKET": analytics_bucket.bucket_name,
            "S3_KEY": "analytics/analytics.duckdb",
            "S3_STORAGE_CLASS": "STANDARD",
            "LOCAL_DB_PATH": "/tmp/analytics.duckdb",  # nosec B108
            "LOCAL_ETAG_PATH": "/tmp/analytics.etag",  # nosec B108
            "EMIT_METRICS": "true",
            "CDC_METRIC_NAMESPACE": "RealityCheckCDC",
            "DEMO_URL": demo_url,
            "COST_WEB_URL": cost_web_url,
        }

        reader = lambda_.Function(
            self,
            "CdcReaderFunction",
            function_name="reality-check-cdc-reader",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="agent.cdc.reader.lambda_handler",
            code=lambda_.Code.from_asset(
                os.path.join(_REPO_ROOT, "agent", "cdc"),
                bundling=self._bundling(),
            ),
            timeout=Duration.seconds(30),
            memory_size=512,
            reserved_concurrent_executions=10,
            log_group=reader_log_group,
            environment=reader_env,
            description="Stateless DuckDB Reader: executes analytical SQL using hybrid /tmp cache + S3.",
        )

        # Reader has read-only access to S3; ZERO access to Aurora/RDS!
        analytics_bucket.grant_read(reader)

        reader_url = reader.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE,
            cors=lambda_.FunctionUrlCorsOptions(
                allowed_origins=["*"],
                allowed_methods=[lambda_.HttpMethod.GET, lambda_.HttpMethod.POST],
                allowed_headers=["*"],
            ),
        )

        # --- 6. CloudWatch Alarms -------------------------------------------
        lag_alarm = cw.Alarm(
            self,
            "CdcReplicationLagHighAlarm",
            alarm_name="RealityCheck-CDC-ReplicationLagHigh",
            metric=cw.Metric(
                namespace="RealityCheckCDC",
                metric_name="ReplicationLagSeconds",
                statistic="Maximum",
                period=Duration.minutes(5),
            ),
            threshold=900,  # 15 minutes lag threshold
            evaluation_periods=2,
            datapoints_to_alarm=2,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            alarm_description="Triggers if DuckDB CDC replication lag exceeds 15 minutes.",
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )

        reinit_alarm = cw.Alarm(
            self,
            "CdcFullReInitSpikeAlarm",
            alarm_name="RealityCheck-CDC-FullReInitSpike",
            metric=cw.Metric(
                namespace="RealityCheckCDC",
                metric_name="FullReInitCount",
                statistic="Sum",
                period=Duration.hours(1),
            ),
            threshold=2,  # Alert if >= 2 full re-inits occur in 1 hour
            evaluation_periods=1,
            datapoints_to_alarm=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description="Triggers if multiple unexpected full table re-inits occur due to drift.",
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )

        # --- Outputs ---------------------------------------------------------
        CfnOutput(self, "LockTableName", value=lock_table.table_name)
        CfnOutput(self, "AnalyticsBucketName", value=analytics_bucket.bucket_name)
        CfnOutput(self, "SyncWorkerFunctionName", value=sync_worker.function_name)
        CfnOutput(self, "ReaderFunctionName", value=reader.function_name)
        CfnOutput(self, "ReaderFunctionUrl", value=reader_url.url)

    @staticmethod
    def _bundling() -> BundlingOptions:
        """Package agent/cdc + duckdb/psycopg dependencies for arm64."""
        docker_cmd = " && ".join([
            "mkdir -p /asset-output/agent/cdc",
            "touch /asset-output/agent/__init__.py",
            "cp -r /asset-input/. /asset-output/agent/cdc/",
            "pip install -r /asset-output/agent/cdc/requirements.txt -t /asset-output "
            "--platform manylinux2014_aarch64 --implementation cp "
            "--python-version 3.12 --only-binary=:all: --upgrade",
            "find /asset-output -name __pycache__ -type d -prune -exec rm -rf {} + || true",
        ])
        return BundlingOptions(
            image=DockerImage.from_registry("public.ecr.aws/lambda/python:3.12-arm64"),
            command=["bash", "-c", docker_cmd],
            local=_LocalCdcBundler(),
        )


@jsii.implements(ILocalBundling)
class _LocalCdcBundler:
    """Local packaging: installs pre-built manylinux aarch64 wheels."""

    def try_bundle(self, output_dir: str, options) -> bool:  # noqa: ANN001
        import shutil
        import subprocess  # nosec B404
        src = os.path.join(_REPO_ROOT, "agent", "cdc")
        try:
            # 1) Copy cdc package
            dest_cdc = os.path.join(output_dir, "agent", "cdc")
            os.makedirs(dest_cdc, exist_ok=True)
            with open(os.path.join(output_dir, "agent", "__init__.py"), "w") as f:
                f.write("")
            shutil.copytree(src, dest_cdc, dirs_exist_ok=True)

            # 2) Install wheels
            reqs_file = os.path.join(dest_cdc, "requirements.txt")
            if os.path.exists(reqs_file):
                subprocess.run(  # nosec B603, B607
                    [
                        "pip", "install",
                        "-r", reqs_file,
                        "-t", output_dir,
                        "--platform", "manylinux2014_aarch64",
                        "--implementation", "cp",
                        "--python-version", "3.12",
                        "--only-binary=:all:",
                        "--upgrade",
                    ],
                    check=True,
                )
            # 3) Cleanup __pycache__
            subprocess.run(  # nosec B603, B607
                ["bash", "-c",
                 f"find '{output_dir}' -name __pycache__ -type d -prune -exec rm -rf {{}} + || true"],
                check=False,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            return False
