"""
ObservabilityStack — CloudWatch dashboard + baseline alarms for Reality Check.

Visualizes what operations actually watches, one compact row per question:
  1. Lambda service health (invocations, errors, throttles, duration, concurrency)
  2. Review throughput + the Bedrock dependency (reviews, claims, invokes, tokens)
  3. Review KPIs (results by outcome, risks flagged, claim-severity mix)
  4. A synthetic hourly canary probing the live demo end-to-end
  5. Aurora Serverless v2 cost-infra (DB connect latency, cold-resume behavior)

App metrics live in the "RealityCheck" namespace (box/telemetry.py); cost-infra
metrics live in "RealityCheckCost" (agent/cost_lambda/metrics.py). Both are
emitted as EMF when the functions run with EMIT_METRICS=true. AWS/Lambda metrics
are published automatically by the service.

Layout: each row is three graph widgets (height 5) followed by ONE full-width
caption strip (height 1). CloudWatch text widgets render only a Markdown subset
(no raw HTML), so captions are plain Markdown kept to a single dense line.
"""
from __future__ import annotations

from typing import List, Optional

from aws_cdk import (
    Duration,
    Stack,
    CfnOutput,
)
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from constructs import Construct

NAMESPACE = "RealityCheck"
COST_NAMESPACE = "RealityCheckCost"

# Lambda's configured timeout (agent/infra/setup.md: --timeout 60).
LAMBDA_TIMEOUT_MS = 60_000

# Consistent colors so risk/outcome semantics read the same across widgets.
GREEN = "#2ca02c"
AMBER = "#ff7f0e"
RED = "#d62728"
BLUE = "#1f77b4"
PURPLE = "#9467bd"
BROWN = "#8c564b"
PINK = "#e377c2"


class ObservabilityStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        function_name: str,
        dashboard_name: str = "RealityCheck",
        alarm_email: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.function_name = function_name
        lambda_dims = {"FunctionName": function_name}

        # --- Optional alarm notifications -------------------------------
        alarm_topic: Optional[sns.Topic] = None
        if alarm_email:
            alarm_topic = sns.Topic(self, "AlarmTopic",
                                    display_name=f"{dashboard_name} alarms")
            alarm_topic.add_subscription(subs.EmailSubscription(alarm_email))

        # --- Baseline operational alarms --------------------------------
        self._build_alarms(lambda_dims, alarm_topic, dashboard_name)

        # --- Dashboard ---------------------------------------------------
        dashboard = cw.Dashboard(
            self,
            "OpsDashboard",
            dashboard_name=dashboard_name,
            default_interval=Duration.hours(3),
        )

        dashboard.add_widgets(
            cw.TextWidget(
                markdown=(
                    f"# Reality Check &mdash; Ops & Utilization\n"
                    f"Lambda **`{function_name}`** &nbsp;·&nbsp; region **`{self.region}`** "
                    f"&nbsp;·&nbsp; metrics **`{NAMESPACE}`** / **`{COST_NAMESPACE}`** "
                    "(emitted when functions run with `EMIT_METRICS=true`)\n\n"
                    "**1** Service health &nbsp;·&nbsp; **2** Throughput & Bedrock "
                    "&nbsp;·&nbsp; **3** Review KPIs &nbsp;·&nbsp; **4** Canary "
                    "&nbsp;·&nbsp; **5** Aurora cost-infra"
                ),
                width=24,
                height=2,
            )
        )

        # Row 1 — Lambda health
        dashboard.add_widgets(
            self._lambda_invocations_widget(lambda_dims),
            self._lambda_duration_widget(lambda_dims),
            self._lambda_concurrency_widget(lambda_dims),
        )
        dashboard.add_widgets(self._legend(
            ("Invocations / Errors / Throttles",
             "errors or throttles > 0 = work is being dropped"),
            ("Duration p50 / p99 vs 60s",
             "p99 near the red line = runs about to time out"),
            ("Max concurrency",
             "nearing the account limit risks throttling"),
        ))

        # Row 2 — review throughput + Bedrock dependency
        dashboard.add_widgets(
            self._actions_widget(),
            self._box_ai_widget(),
            self._bedrock_widget(),
        )
        dashboard.add_widgets(self._legend(
            ("Contract reviews run",
             "reviews vs. no-match; rising no-match = ingestion drift"),
            ("Claims reviewed",
             "claims checked — the latency & token-spend driver"),
            ("Bedrock invokes / failures / tokens",
             "failures = degraded analysis; tokens = model spend"),
        ))

        # Row 3 — review KPIs
        dashboard.add_widgets(
            self._workflow_outcome_widget(),
            self._human_loop_widget(),
            self._risk_mix_widget(),
        )
        dashboard.add_widgets(self._legend(
            ("Claims by result",
             "no-risk / risk-identified / no-source-found"),
            ("Risks flagged",
             "risk-identified count = the human-review workload"),
            ("Claim severity mix",
             "High / Medium / None; High-heavy = risky terms"),
        ))

        # Row 4 — hourly canary (synthetic end-to-end probe of the live demo)
        dashboard.add_widgets(
            self._canary_status_widget(),
            self._canary_latency_widget(),
            self._canary_checks_widget(),
        )
        dashboard.add_widgets(self._legend(
            ("Canary success / failure (hourly)",
             "any failure = public endpoint broken, Lambda looks fine"),
            ("Canary latency by endpoint (p90)",
             "round-trip per route; rising = early warning"),
            ("Canary checks pass / fail",
             "per-endpoint, points at the broken route"),
        ))

        # Row 5 — Aurora Serverless v2 cost-infra. Tracks how long DB connects
        # take and how often a cold resume is hit, so the resume penalty can be
        # watched over time. Metrics come from the cost Lambdas (COST_NAMESPACE).
        dashboard.add_widgets(
            self._db_connect_latency_widget(),
            self._db_cold_resume_widget(),
            self._db_connect_failure_widget(),
        )
        dashboard.add_widgets(self._legend(
            ("DB connect latency (p50 / p99)",
             "cold resume from auto-pause spikes p99 toward ~15s"),
            ("Cold-resume retries",
             "connections that hit a paused cluster and retried"),
            ("DB connect failures",
             "should be zero; sustained = resume exceeds timeout"),
        ))

        CfnOutput(
            self,
            "DashboardURL",
            value=(
                f"https://{self.region}.console.aws.amazon.com/cloudwatch/home"
                f"?region={self.region}#dashboards:name={dashboard_name}"
            ),
            description="Direct link to the CloudWatch dashboard.",
        )

    # ------------------------------------------------------------------ #
    # Widget helpers
    # ------------------------------------------------------------------ #
    def _legend(self, *items: tuple[str, str]) -> cw.TextWidget:
        """One compact, full-width caption strip for a whole row of three
        widgets. CloudWatch text widgets only render a Markdown subset (no raw
        HTML such as <details>), so we use plain Markdown: each item is a bold
        title and a short note, separated by a mid-dot. A single height=1 strip
        under the row keeps the dashboard dense while staying readable."""
        cells = " &nbsp;&nbsp;·&nbsp;&nbsp; ".join(
            f"**{title}** — {note}" for title, note in items
        )
        return cw.TextWidget(markdown=cells, width=24, height=1)

    # ------------------------------------------------------------------ #
    # Metric helpers
    # ------------------------------------------------------------------ #
    def _lambda_metric(self, metric_name: str, dims: dict,
                       stat: str = "Sum") -> cw.Metric:
        return cw.Metric(
            namespace="AWS/Lambda",
            metric_name=metric_name,
            dimensions_map=dims,
            statistic=stat,
            period=Duration.minutes(5),
        )

    def _rc_metric(self, metric_name: str, *, dims: Optional[dict] = None,
                   stat: str = "Sum", label: Optional[str] = None,
                   color: Optional[str] = None) -> cw.Metric:
        return cw.Metric(
            namespace=NAMESPACE,
            metric_name=metric_name,
            dimensions_map=dims or {},
            statistic=stat,
            period=Duration.minutes(5),
            label=label,
            color=color,
        )

    def _cost_metric(self, metric_name: str, *, stat: str = "Average",
                     label: Optional[str] = None,
                     color: Optional[str] = None) -> cw.Metric:
        return cw.Metric(
            namespace=COST_NAMESPACE,
            metric_name=metric_name,
            statistic=stat,
            period=Duration.hours(1),
            label=label,
            color=color,
        )

    # ------------------------------------------------------------------ #
    # Row 1 — Lambda health
    # ------------------------------------------------------------------ #
    def _lambda_invocations_widget(self, dims: dict) -> cw.GraphWidget:
        return cw.GraphWidget(
            title="Lambda invocations & errors",
            width=8,
            height=4,
            left=[
                self._lambda_metric("Invocations", dims),
                self._lambda_metric("Errors", dims),
                self._lambda_metric("Throttles", dims),
            ],
            left_y_axis=cw.YAxisProps(min=0),
        )

    def _lambda_duration_widget(self, dims: dict) -> cw.GraphWidget:
        return cw.GraphWidget(
            title="Lambda duration (p50 / p99) vs 60s timeout",
            width=8,
            height=4,
            left=[
                self._lambda_metric("Duration", dims, stat="p50"),
                self._lambda_metric("Duration", dims, stat="p99"),
            ],
            left_y_axis=cw.YAxisProps(min=0, label="ms"),
            left_annotations=[
                cw.HorizontalAnnotation(
                    value=LAMBDA_TIMEOUT_MS, label="60s timeout", color=RED
                )
            ],
        )

    def _lambda_concurrency_widget(self, dims: dict) -> cw.GraphWidget:
        return cw.GraphWidget(
            title="Concurrency",
            width=8,
            height=4,
            left=[self._lambda_metric("ConcurrentExecutions", dims, stat="Maximum")],
            left_y_axis=cw.YAxisProps(min=0),
        )

    # ------------------------------------------------------------------ #
    # Row 2 — review throughput + Bedrock dependency
    # ------------------------------------------------------------------ #
    def _actions_widget(self) -> cw.GraphWidget:
        return cw.GraphWidget(
            title="Contract reviews run",
            width=8,
            height=4,
            left=[
                self._rc_metric("ReviewRun", label="reviews", color=BLUE),
                self._rc_metric("ReviewNoMatch", label="no-match", color=AMBER),
            ],
            left_y_axis=cw.YAxisProps(min=0),
        )

    def _box_ai_widget(self) -> cw.GraphWidget:
        return cw.GraphWidget(
            title="Claims reviewed",
            width=8,
            height=4,
            left=[
                self._rc_metric("ReviewClaims", stat="Sum", label="claims", color=BLUE),
            ],
            left_y_axis=cw.YAxisProps(min=0),
        )

    def _bedrock_widget(self) -> cw.GraphWidget:
        return cw.GraphWidget(
            title="Bedrock: invokes, failures & tokens",
            width=8,
            height=4,
            left=[
                self._rc_metric("BedrockInvoke", label="invokes", color=BLUE),
                self._rc_metric("BedrockInvokeFailure", label="failures", color=RED),
            ],
            right=[
                self._rc_metric("BedrockInputTokens", label="input tokens", color=BROWN),
                self._rc_metric("BedrockOutputTokens", label="output tokens", color=PINK),
            ],
            left_y_axis=cw.YAxisProps(min=0, label="calls"),
            right_y_axis=cw.YAxisProps(min=0, label="tokens"),
        )

    # ------------------------------------------------------------------ #
    # Row 3 — review KPIs
    # ------------------------------------------------------------------ #
    def _workflow_outcome_widget(self) -> cw.GraphWidget:
        return cw.GraphWidget(
            title="Claims by result",
            width=8,
            height=4,
            stacked=True,
            left=[
                self._rc_metric("ReviewResult", dims={"result": "No risk"},
                                label="no risk", color=GREEN),
                self._rc_metric("ReviewResult", dims={"result": "Risk identified"},
                                label="risk identified", color=RED),
                self._rc_metric("ReviewResult", dims={"result": "No source found"},
                                label="no source found", color=AMBER),
            ],
            left_y_axis=cw.YAxisProps(min=0),
        )

    def _human_loop_widget(self) -> cw.GraphWidget:
        return cw.GraphWidget(
            title="Risks found vs clean reviews",
            width=8,
            height=4,
            left=[
                self._rc_metric("ReviewResult", dims={"result": "Risk identified"},
                                stat="Sum", label="risks flagged", color=RED),
            ],
            left_y_axis=cw.YAxisProps(min=0),
        )

    def _risk_mix_widget(self) -> cw.GraphWidget:
        return cw.GraphWidget(
            title="Claim severity mix",
            width=8,
            height=4,
            stacked=True,
            left=[
                self._rc_metric("ReviewSeverity", dims={"severity": "High"},
                                label="High", color=RED),
                self._rc_metric("ReviewSeverity", dims={"severity": "Medium"},
                                label="Medium", color=AMBER),
                self._rc_metric("ReviewSeverity", dims={"severity": "None"},
                                label="None", color=GREEN),
            ],
            left_y_axis=cw.YAxisProps(min=0),
        )

    # ------------------------------------------------------------------ #
    # Row 4 — hourly canary
    # ------------------------------------------------------------------ #
    def _canary_status_widget(self) -> cw.GraphWidget:
        return cw.GraphWidget(
            title="Canary success / failure (hourly)",
            width=8,
            height=4,
            stacked=True,
            left=[
                self._rc_metric("CanarySuccess", label="success", color=GREEN),
                self._rc_metric("CanaryFailure", label="failure", color=RED),
            ],
            left_y_axis=cw.YAxisProps(min=0),
        )

    def _canary_latency_widget(self) -> cw.GraphWidget:
        endpoints = ["healthz", "landing", "review", "report_pdf"]
        return cw.GraphWidget(
            title="Canary latency by endpoint (p90, ms)",
            width=8,
            height=4,
            left=[
                self._rc_metric("CanaryLatencyMs", dims={"endpoint": e},
                                stat="p90", label=e)
                for e in endpoints
            ],
            left_y_axis=cw.YAxisProps(min=0, label="ms"),
        )

    def _canary_checks_widget(self) -> cw.GraphWidget:
        return cw.GraphWidget(
            title="Canary checks pass / fail",
            width=8,
            height=4,
            left=[
                self._rc_metric("CanaryCheck", dims={"result": "pass"},
                                label="pass", color=GREEN),
                self._rc_metric("CanaryCheck", dims={"result": "fail"},
                                label="fail", color=RED),
            ],
            left_y_axis=cw.YAxisProps(min=0),
        )

    # ------------------------------------------------------------------ #
    # Row 5 — Aurora Serverless v2 cost-infra (RealityCheckCost namespace)
    # ------------------------------------------------------------------ #
    def _db_connect_latency_widget(self) -> cw.GraphWidget:
        return cw.GraphWidget(
            title="DB connect latency (p50 / p99) — Aurora resume",
            width=8,
            height=4,
            left=[
                self._cost_metric("DbConnectLatencyMs", stat="p50",
                                  label="p50", color=BLUE),
                self._cost_metric("DbConnectLatencyMs", stat="p99",
                                  label="p99", color=AMBER),
            ],
            left_y_axis=cw.YAxisProps(min=0, label="ms"),
            left_annotations=[
                cw.HorizontalAnnotation(value=15000, label="~15s cold resume",
                                        color=RED),
            ],
        )

    def _db_cold_resume_widget(self) -> cw.GraphWidget:
        return cw.GraphWidget(
            title="Cold-resume retries",
            width=8,
            height=4,
            left=[
                self._cost_metric("DbColdResume", stat="Sum",
                                  label="cold resumes", color=AMBER),
            ],
            left_y_axis=cw.YAxisProps(min=0),
        )

    def _db_connect_failure_widget(self) -> cw.GraphWidget:
        return cw.GraphWidget(
            title="DB connect failures",
            width=8,
            height=4,
            left=[
                self._cost_metric("DbConnectFailure", stat="Sum",
                                  label="failures", color=RED),
            ],
            left_y_axis=cw.YAxisProps(min=0),
        )

    # ------------------------------------------------------------------ #
    # Alarms
    # ------------------------------------------------------------------ #
    def _build_alarms(self, lambda_dims: dict,
                      topic: Optional[sns.Topic],
                      dashboard_name: str = "RealityCheck") -> None:
        alarms: List[cw.Alarm] = []

        alarms.append(
            cw.Alarm(
                self,
                "LambdaErrorsAlarm",
                alarm_description="Lambda function is returning errors.",
                metric=self._lambda_metric("Errors", lambda_dims),
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
        )

        alarms.append(
            cw.Alarm(
                self,
                "LambdaDurationP99Alarm",
                alarm_description="p99 duration approaching the 60s timeout.",
                metric=self._lambda_metric("Duration", lambda_dims, stat="p99"),
                threshold=LAMBDA_TIMEOUT_MS * 0.83,  # ~50s
                evaluation_periods=3,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
        )

        alarms.append(
            cw.Alarm(
                self,
                "BedrockInvokeFailuresAlarm",
                alarm_description="Bedrock model invocations are failing (LLM review degraded).",
                metric=self._rc_metric("BedrockInvokeFailure"),
                threshold=3,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
        )

        # The hourly canary probes the live demo end-to-end. Any failed run means
        # the public endpoint is broken even if raw Lambda metrics look fine, so
        # alarm on the first failure. The metric is evaluated over a 1h period to
        # match the canary cadence; a run that never happened (missing data) is
        # surfaced as breaching so a silent/stopped canary also alarms.
        alarms.append(
            cw.Alarm(
                self,
                "CanaryFailureAlarm",
                alarm_name=f"{dashboard_name}-CanaryFailure",
                alarm_description=(
                    "The hourly web-demo canary reported a failed run (an "
                    "end-to-end check against the live Function URL failed), or "
                    "no canary run was observed in the last hour."
                ),
                metric=self._rc_metric("CanaryFailure", stat="Sum").with_(
                    period=Duration.hours(1)),
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.BREACHING,
            )
        )

        if topic is not None:
            action = cw_actions.SnsAction(topic)
            for alarm in alarms:
                alarm.add_alarm_action(action)
