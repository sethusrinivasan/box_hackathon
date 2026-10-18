#!/usr/bin/env python3
"""
CDK app entry point for Reality Check infrastructure.

Provisions two stacks, replicable in any account/region (config via CDK context):

  * RealityCheckWebDemo       — the public, no-auth Lambda Function URL demo
                               (offline contract-review pipeline) with an
                               explicit, least-privilege execution role.
  * RealityCheckObservability — CloudWatch dashboard + baseline alarms over the
                               web demo and the RealityCheck metric namespace.

Region note: this account uses the "new AWS experience" where all regional
resources must live in the project's single assigned Region. We read the target
account/region from CDK_DEFAULT_ACCOUNT / CDK_DEFAULT_REGION rather than
hardcoding.

Context keys (all optional):
  functionName     web-demo Lambda name (default reality-check-web-demo)
  dashboardName    CloudWatch dashboard name (default RealityCheck)
  alarmEmail       SNS email for alarms (default: none)
  webConcurrency   reserved concurrency cap (default 5)
"""
import os

import aws_cdk as cdk

from reality_check_infra.observability_stack import ObservabilityStack
from reality_check_infra.web_demo_stack import WebDemoStack

app = cdk.App()

function_name = app.node.try_get_context("functionName") or "reality-check-web-demo"
dashboard_name = app.node.try_get_context("dashboardName") or "RealityCheck"
alarm_email = app.node.try_get_context("alarmEmail")  # optional
web_concurrency = int(app.node.try_get_context("webConcurrency") or 5)

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    # Frugal region default: us-east-1 (N. Virginia) is AWS's lowest-cost
    # standard region and has the widest Bedrock model availability (incl.
    # Amazon Nova Lite). Lambda/S3/CloudWatch are the same price in us-east-1
    # and us-east-2, so an already-deployed us-east-2 stack is fine to leave
    # in place. Single-region, no cross-region redundancy by design — the demo
    # does not need it and redundancy would only add cost.
    region=(os.environ.get("CDK_DEFAULT_REGION") or "us-east-1"),
)

WebDemoStack(
    app,
    "RealityCheckWebDemo",
    function_name=function_name,
    reserved_concurrency=web_concurrency,
    env=env,
    description="Reality Check public web demo: Lambda Function URL (no auth, offline).",
)

ObservabilityStack(
    app,
    "RealityCheckObservability",
    function_name=function_name,       # dashboard targets the web-demo function
    dashboard_name=dashboard_name,
    alarm_email=alarm_email,
    env=env,
    description="Reality Check observability: CloudWatch dashboard + baseline alarms.",
)

app.synth()
