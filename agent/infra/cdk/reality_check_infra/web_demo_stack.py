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
)
from constructs import Construct

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
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        log_group = logs.LogGroup(
            self, "WebDemoLogGroup",
            log_group_name=f"/aws/lambda/{function_name}",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Explicit least-privilege execution role: logs to its own group only.
        role = iam.Role(
            self, "WebDemoExecutionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Least-privilege execution role for the Reality Check web demo.",
        )
        log_group.grant_write(role)

        fn = lambda_.Function(
            self, "WebDemoFunction",
            function_name=function_name,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="agent.web_handler.lambda_handler",
            code=lambda_.Code.from_asset(_REPO_ROOT, bundling=self._bundling()),
            role=role,
            log_group=log_group,
            timeout=Duration.seconds(30),
            memory_size=512,
            reserved_concurrent_executions=reserved_concurrency,
            environment={
                "LOG_LEVEL": "INFO",
                "EMIT_METRICS": "true",
                # /var/task is read-only in Lambda; route report/comment writes to /tmp.
                "REALITY_CHECK_REPORTS_DIR": "/tmp/rc-reports",
            },
            description="Public offline Reality Check demo (no auth).",
        )

        url = fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE,
            cors=lambda_.FunctionUrlCorsOptions(
                allowed_origins=["*"],
                allowed_methods=[lambda_.HttpMethod.GET, lambda_.HttpMethod.POST],
            ),
        )

        CfnOutput(self, "WebDemoUrl", value=url.url,
                  description="Public demo URL (open in a browser).")
        CfnOutput(self, "WebDemoFunctionName", value=fn.function_name,
                  description="Lambda function name for the demo.")

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
            image=DockerImage.from_registry("public.ecr.aws/lambda/python:3.12"),
            command=["bash", "-c", cmd],
            local=_LocalBundler(cmd),
        )


@jsii.implements(ILocalBundling)
class _LocalBundler:
    """Run the copy on the host instead of Docker when possible."""

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
