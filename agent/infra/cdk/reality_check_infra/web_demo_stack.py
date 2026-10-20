"""
WebDemoStack — public, no-auth Lambda Function URL serving the Reality Check demo.

Packages the post-restructure runtime (agent/web_handler.py + agent/local_agent.py
+ box/ + the docs/box_upload fixtures) into a single Lambda and exposes it on a
Function URL with `authType = NONE`. The function runs the OFFLINE contract-review
pipeline only (web_handler forces LocalDocs + a canned Bedrock client), so there
is nothing sensitive behind the endpoint.

IAM / credentials posture (least privilege, no creds passed between services):
  * Explicit execution role — not the CDK-default one — with minimal grants.
  * CloudWatch Logs scoped to this function's own log group.
  * The offline demo needs no Box/Bedrock access at all, so the role has only
    the logs grant. (Live deployments would add scoped grants; not needed here.)

Guardrails:
  * reserved_concurrent_executions caps abuse cost on a public endpoint.
  * EMIT_METRICS=true feeds the dashboard; writes routed to /tmp.
"""
from __future__ import annotations

import os
from typing import Optional

import jsii
from aws_cdk import (
    Duration,
    Stack,
    CfnOutput,
    BundlingOptions,
    ILocalBundling,
    DockerImage,
    RemovalPolicy,
    aws_lambda as lambda_,
    aws_iam as iam,
    aws_logs as logs,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
)
from constructs import Construct

# Inline source for the idle-parker Lambda. When the "no invocations for 3 days"
# alarm fires, SNS invokes this function, which sets the demo function's reserved
# concurrency to 0 — i.e. parks it (requests get 429, compute cost goes to $0).
# Recovery is manual by design: re-enable with
#   aws lambda put-function-concurrency --function-name <name> --reserved-concurrent-executions 5
# so a stray bot request can never silently un-park (and re-start cost).
_PARKER_SRC = '''
import json, os, boto3

def handler(event, context):
    target = os.environ["TARGET_FUNCTION"]
    lam = boto3.client("lambda")
    lam.put_function_concurrency(
        FunctionName=target,
        ReservedConcurrentExecutions=0,
    )
    print(json.dumps({"action": "parked", "function": target,
                      "reserved_concurrency": 0,
                      "reason": "no invocations for the idle window"}))
    return {"parked": target}
'''

# Repo root = four levels up: reality_check_infra/ -> cdk/ -> infra/ -> agent/ -> root
_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)


class WebDemoStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        function_name: str = "reality-check-web-demo",
        reserved_concurrency: int = 5,
        cost_web_url: str = "",
        flag_param_name: str = "/reality-check/web-demo/evidence-store",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        log_group = logs.LogGroup(
            self, "WebDemoLogGroup",
            log_group_name=f"/aws/lambda/{function_name}",
            # Frugal: logs are diagnostic-only for an offline demo; 3 days is
            # enough to debug a bad deploy and keeps CloudWatch Logs storage
            # at effectively zero.
            retention=logs.RetentionDays.THREE_DAYS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Explicit least-privilege execution role: logs to its own group only.
        role = iam.Role(
            self, "WebDemoExecutionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Least-privilege execution role for the Reality Check web demo.",
        )
        log_group.grant_write(role)

        # AWS-Native S3 storage for contracts, evidence, and reports
        data_bucket = s3.Bucket(
            self, "DataBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
        )

        # Seed the bucket with baseline evidence & sample contracts
        s3deploy.BucketDeployment(
            self, "DeployFixtures",
            sources=[s3deploy.Source.asset(os.path.join(_REPO_ROOT, "docs", "box_upload"))],
            destination_bucket=data_bucket,
        )

        # Grant read & write access to the Lambda execution role
        data_bucket.grant_read_write(role)

        # Allow Bedrock invocations for live AWS-native review mode
        role.add_to_policy(iam.PolicyStatement(
            sid="BedrockInvokeModelAccess",
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=["*"],
        ))

        fn = lambda_.Function(
            self, "WebDemoFunction",
            function_name=function_name,
            runtime=lambda_.Runtime.PYTHON_3_12,
            # Frugal: Graviton2 (arm64) is ~20% cheaper per GB-s than x86_64 and
            # the offline pipeline is pure Python, so it runs unchanged on arm.
            architecture=lambda_.Architecture.ARM_64,
            handler="agent.web_handler.lambda_handler",
            code=lambda_.Code.from_asset(_REPO_ROOT, bundling=self._bundling()),
            role=role,
            log_group=log_group,
            timeout=Duration.seconds(60),
            memory_size=256,
            reserved_concurrent_executions=reserved_concurrency,
            environment={
                "LOG_LEVEL": "INFO",
                "EMIT_METRICS": "true",
                # /var/task is read-only in Lambda; route report/comment writes
                # to /tmp (the only writable dir in Lambda).
                "REALITY_CHECK_REPORTS_DIR": "/tmp/rc-reports",  # nosec B108
                # When set, the demo page shows a "Cloud cost" link to the cost
                # web view (RealityCheckCostTracker stack's CostWebUrl output).
                "COST_WEB_URL": cost_web_url,
                # SSM-backed feature flag the demo page reads/flips (pgvector
                # evidence retrieval on|off). Parameter name only — no secret.
                "EVIDENCE_FLAG_PARAM": flag_param_name,
                # AWS-Native S3 integration
                "REALITY_CHECK_S3_BUCKET": data_bucket.bucket_name,
                "BEDROCK_ENABLED": "true",
                # Frugal default model on Amazon Bedrock ($0.06/$0.24 per 1M tokens)
                "BEDROCK_MODEL_ID": "us.amazon.nova-lite-v1:0",
            },
            description="Public Reality Check demo & custom web portal.",
        )

        # --- Feature-flag access (scoped, calculated-risk) ------------------
        # The public page both reads and flips ONE SSM parameter. Grant exactly
        # ssm:GetParameter + PutParameter on that single parameter ARN — no
        # wildcards, no other SSM access. Blast radius is one boolean in
        # {on, off}: no data exposure, no privilege escalation. Acceptable for a
        # public demo toggle; a production flag would gate writes behind auth.
        role.add_to_policy(iam.PolicyStatement(
            sid="DemoFeatureFlagReadWrite",
            actions=["ssm:GetParameter", "ssm:PutParameter"],
            resources=[
                f"arn:aws:ssm:{self.region}:{self.account}:parameter{flag_param_name}"
            ],
        ))

        url = fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE,
            cors=lambda_.FunctionUrlCorsOptions(
                allowed_origins=["*"],
                allowed_methods=[lambda_.HttpMethod.GET, lambda_.HttpMethod.POST],
            ),
        )

        # --- Auto-shutdown on inactivity -------------------------------
        # Fully on-demand already (Lambda scales to zero), but the public URL
        # stays reachable forever. This parks the function after 3 days with no
        # invocations so an idle demo truly costs $0 and stops serving bots.
        self._add_idle_parker(fn, idle_days=3)

        CfnOutput(self, "WebDemoUrl", value=url.url,
                  description="Public demo URL (open in a browser).")
        CfnOutput(self, "WebDemoFunctionName", value=fn.function_name,
                  description="Lambda function name for the demo.")
        CfnOutput(self, "DataBucketName", value=data_bucket.bucket_name,
                  description="S3 bucket for AWS-native contracts, evidence, and reports.")
        CfnOutput(self, "ReEnableCommand",
                  value=(f"aws lambda put-function-concurrency "
                         f"--function-name {function_name} "
                         f"--reserved-concurrent-executions {reserved_concurrency}"),
                  description="Run this to un-park the demo after an idle shutdown.")

    def _add_idle_parker(self, fn: lambda_.Function, *, idle_days: int) -> None:
        """Park `fn` (reserved concurrency -> 0) when it has had zero
        invocations for `idle_days`. Serverless and near-zero cost: the parker
        runs only on the alarm transition. Recovery is manual (see output)."""
        parker = lambda_.Function(
            self, "IdleParkerFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="index.handler",
            code=lambda_.Code.from_inline(_PARKER_SRC),
            timeout=Duration.seconds(30),
            memory_size=128,
            environment={"TARGET_FUNCTION": fn.function_name},
            description="Parks the web demo (reserved concurrency -> 0) when idle.",
        )
        # Least privilege: only allowed to throttle the one demo function.
        parker.add_to_role_policy(iam.PolicyStatement(
            actions=["lambda:PutFunctionConcurrency"],
            resources=[fn.function_arn],
        ))
        # Short log retention for the parker too.
        logs.LogGroup(
            self, "IdleParkerLogGroup",
            log_group_name=f"/aws/lambda/{parker.function_name}",
            retention=logs.RetentionDays.THREE_DAYS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        topic = sns.Topic(self, "IdleShutdownTopic",
                          display_name="Reality Check idle shutdown")
        topic.add_subscription(subs.LambdaSubscription(parker))

        # Alarm: Invocations summed per day == 0 for `idle_days` consecutive days.
        # treat_missing_data=BREACHING so "no datapoints" (truly idle) counts as
        # breaching and triggers the shutdown.
        idle_alarm = cw.Alarm(
            self, "IdleInvocationsAlarm",
            alarm_description=(
                f"Web demo has had zero invocations for {idle_days} days — "
                "parking it to $0."
            ),
            metric=cw.Metric(
                namespace="AWS/Lambda",
                metric_name="Invocations",
                dimensions_map={"FunctionName": fn.function_name},
                statistic="Sum",
                period=Duration.days(1),
            ),
            threshold=1,
            evaluation_periods=idle_days,
            datapoints_to_alarm=idle_days,
            comparison_operator=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.BREACHING,
        )
        idle_alarm.add_alarm_action(cw_actions.SnsAction(topic))

    @staticmethod
    def _bundling() -> BundlingOptions:
        """Assemble the Lambda package from the repo, preserving the layout the
        handler imports: agent/*.py (web_handler, local_agent, minipdf, envload),
        box/ (telemetry, box_client), and the docs/box_upload fixtures. Pure file
        copy — no pip deps in the offline path — so local bundling is used, with
        Docker as the CDK-required fallback."""
        cmd = " && ".join([
            "mkdir -p /asset-output/agent /asset-output/box /asset-output/docs/box_upload",
            # agent runtime modules (flat layout; skip tests, infra, caches)
            "cp agent/*.py /asset-output/agent/",
            "rm -f /asset-output/agent/test_*.py",
            # box client + telemetry
            "cp box/*.py /asset-output/box/",
            # offline fixtures (contracts + evidence PDFs)
            "cp -r docs/box_upload/contracts /asset-output/docs/box_upload/contracts",
            "cp -r docs/box_upload/evidence /asset-output/docs/box_upload/evidence",
            # package markers so `agent.web_handler` imports as a module
            "touch /asset-output/agent/__init__.py",
            "find /asset-output -name __pycache__ -type d -prune -exec rm -rf {} + || true",
        ])
        return BundlingOptions(
            # arm64 image to match the function's Graviton architecture (only
            # used as the Docker fallback; the pure-file copy runs locally).
            image=DockerImage.from_registry("public.ecr.aws/lambda/python:3.12-arm64"),
            command=["bash", "-c", cmd],
            local=_LocalBundler(cmd),
        )


@jsii.implements(ILocalBundling)
class _LocalBundler:
    """Run the copy on the host instead of Docker when possible."""

    def __init__(self, cmd: str) -> None:
        self._cmd = cmd

    def try_bundle(self, output_dir: str, options) -> bool:  # noqa: ANN001
        # CDK local bundling runs a fixed build command.
        import subprocess  # nosec B404
        cmd = self._cmd.replace("/asset-output", output_dir)
        try:
            # Fixed bash build command (no user input, no shell=True); partial
            # path "bash" is the standard CDK bundler pattern.
            subprocess.run(["bash", "-c", cmd], cwd=_REPO_ROOT, check=True)  # nosec B603 B607
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False
