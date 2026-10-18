"""
CanaryStack — hourly synthetic canary for the public web demo.

An EventBridge rule invokes a small Lambda (agent/canary.py) once an hour. The
canary probes the live Function URL end-to-end (page, health, review, PDF) and
emits CloudWatch metrics (CanaryRun / CanarySuccess / CanaryFailure /
CanaryLatencyMs / CanaryCheck) into the RealityCheck namespace, which the
observability dashboard rolls up.

Least-privilege, frugal, replicable:
  * Explicit execution role; logs scoped to the canary's own log group only.
  * No Box/Bedrock access — the canary only makes outbound HTTPS to the public
    demo URL (no AWS SDK calls needed beyond logs/EMF).
  * arm64 / 128 MB / 3-day log retention; one run/hour is well within free tier.
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
    ILocalBundling,
    DockerImage,
    aws_lambda as lambda_,
    aws_iam as iam,
    aws_logs as logs,
    aws_events as events,
    aws_events_targets as targets,
)
from constructs import Construct

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)


class CanaryStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        demo_url: str,
        function_name: str = "reality-check-canary",
        schedule_minutes: int = 60,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        log_group = logs.LogGroup(
            self, "CanaryLogGroup",
            log_group_name=f"/aws/lambda/{function_name}",
            retention=logs.RetentionDays.THREE_DAYS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        role = iam.Role(
            self, "CanaryExecutionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Least-privilege execution role for the Reality Check canary.",
        )
        log_group.grant_write(role)

        fn = lambda_.Function(
            self, "CanaryFunction",
            function_name=function_name,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="agent.canary.lambda_handler",
            code=lambda_.Code.from_asset(_REPO_ROOT, bundling=self._bundling()),
            role=role,
            log_group=log_group,
            timeout=Duration.seconds(30),
            memory_size=128,
            environment={
                "LOG_LEVEL": "INFO",
                "EMIT_METRICS": "true",
                "DEMO_URL": demo_url,
            },
            description="Hourly synthetic canary for the Reality Check web demo.",
        )

        # Hourly schedule (EventBridge rule → canary Lambda).
        events.Rule(
            self, "CanarySchedule",
            schedule=events.Schedule.rate(Duration.minutes(schedule_minutes)),
            targets=[targets.LambdaFunction(fn)],
            description="Invoke the Reality Check canary on a fixed cadence.",
        )

        CfnOutput(self, "CanaryFunctionName", value=fn.function_name)

    @staticmethod
    def _bundling() -> BundlingOptions:
        """Bundle just what the canary imports: agent/canary.py + box/telemetry.py.
        Pure stdlib (urllib) — no pip deps — so local file-copy bundling."""
        cmd = " && ".join([
            "mkdir -p /asset-output/agent /asset-output/box",
            "cp agent/canary.py /asset-output/agent/",
            "cp box/telemetry.py /asset-output/box/",
            "touch /asset-output/agent/__init__.py",
            "find /asset-output -name __pycache__ -type d -prune -exec rm -rf {} + || true",
        ])
        return BundlingOptions(
            image=DockerImage.from_registry("public.ecr.aws/lambda/python:3.12-arm64"),
            command=["bash", "-c", cmd],
            local=_LocalBundler(cmd),
        )


@jsii.implements(ILocalBundling)
class _LocalBundler:
    def __init__(self, cmd: str) -> None:
        self._cmd = cmd

    def try_bundle(self, output_dir: str, options) -> bool:  # noqa: ANN001
        import subprocess
        cmd = self._cmd.replace("/asset-output", output_dir)
        try:
            subprocess.run(["bash", "-c", cmd], cwd=_REPO_ROOT, check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False
