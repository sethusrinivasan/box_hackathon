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

from reality_check_infra.canary_stack import CanaryStack
from reality_check_infra.observability_stack import ObservabilityStack
from reality_check_infra.web_demo_stack import WebDemoStack

# The cost tracker is developed in a parallel session; import it optionally so
# this app stays synth-able even if that module hasn't been committed yet
# (two sessions share one app.py). When present, its stack is added below.
try:
    from reality_check_infra.cost_tracker_stack import CostTrackerStack
except ImportError:
    CostTrackerStack = None

try:
    from reality_check_infra.cdc_stack import CdcStack
except ImportError:
    CdcStack = None

app = cdk.App()

function_name = app.node.try_get_context("functionName") or "reality-check-web-demo"
dashboard_name = app.node.try_get_context("dashboardName") or "RealityCheck"
alarm_email = app.node.try_get_context("alarmEmail")  # optional
web_concurrency = int(app.node.try_get_context("webConcurrency") or 5)
# Optional: URL of the Cloud-cost web view (CostWebUrl output of the cost-tracker
# stack). When provided, the demo page shows a "Cloud cost" link.
cost_web_url = app.node.try_get_context("costWebUrl") or ""
# Live demo URL the hourly canary probes. Defaults to the deployed Function URL;
# override with -c demoUrl=... for a different deployment.
demo_url = (app.node.try_get_context("demoUrl")
            or "https://2c5gs4u2lsxjzi66dbxbr7iuzu0xhila.lambda-url.us-east-2.on.aws/")

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
    cost_web_url=cost_web_url,
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

# Hourly synthetic canary that probes the live demo end-to-end and emits
# Canary* metrics into the RealityCheck namespace for the dashboard.
CanaryStack(
    app,
    "RealityCheckCanary",
    demo_url=demo_url,
    env=env,
    description="Reality Check hourly canary: EventBridge schedule + probe Lambda.",
)

# Daily holistic cost tracking into Aurora Serverless v2 Postgres created with
# EXPRESS configuration (IAM-auth by default, internet access gateway so no NAT
# and no VPC, single serverless writer, native PG, no stored secrets).
# Added only when the module is present (see the optional import above).
if CostTrackerStack is not None:
    CostTrackerStack(
        app,
        "RealityCheckCostTracker",
        env=env,
        description=("Reality Check daily cost tracker: Aurora Serverless v2 "
                     "PostgreSQL 18 (min 0 ACU) + collector Lambda via IAM DB auth."),
    )

if CdcStack is not None:
    CdcStack(
        app,
        "RealityCheckCdc",
        cluster_endpoint="reality-check-cost-tracker.cluster-c52ymc6u0ruy.us-east-2.rds.amazonaws.com",
        cluster_resource_id="cluster-DJLU4ZBVHZ6HGQCZTXR7YKDQ7Q",
        db_iam_user="cdc_sync_worker",
        env=env,
        description="Reality Check serverless Aurora-to-DuckDB CDC pipeline via S3.",
    )

app.synth()
