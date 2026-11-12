"""The dashboards ReliaLoop ships, keyed by output file name."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tools.dashboards.builder import Layout, Query, Threshold, custom_variable, dashboard

# Operational routes are excluded everywhere, exactly as in the SLO specs.
API = 'route!~"/healthz|/readyz|/metrics|/chaos.*"'
SVC = 'service="$service"'
GREEN, AMBER, RED = "green", "orange", "red"


def _quantile(q: float, selector: str) -> str:
    return (
        f"histogram_quantile({q}, sum by (le) "
        f"(rate(http_request_duration_seconds_bucket{{{selector}}}[1m])))"
    )


def golden_signals() -> dict[str, Any]:
    """Dashboard 1: the four golden signals for one service."""
    layout = Layout()

    layout.row("Overview")
    layout.stat(
        "Request rate",
        "API requests per second over the last minute (operational routes excluded).",
        [Query(f"sum(rate(http_requests_total{{{SVC}, {API}}}[1m]))")],
        unit="reqps",
        decimals=1,
    )
    layout.stat(
        "Error ratio",
        "Share of API requests answered with a 5xx status over the last minute.",
        [
            Query(
                f'sum(rate(http_requests_total{{{SVC}, {API}, status=~"5.."}}[1m])) '
                f"/ sum(rate(http_requests_total{{{SVC}, {API}}}[1m]))"
            )
        ],
        unit="percentunit",
        decimals=2,
        thresholds=[Threshold(None, GREEN), Threshold(0.005, AMBER), Threshold(0.05, RED)],
    )
    layout.stat(
        "p95 latency",
        "95th percentile API request duration over the last minute.",
        [Query(_quantile(0.95, f"{SVC}, {API}"))],
        unit="s",
        decimals=3,
        thresholds=[Threshold(None, GREEN), Threshold(0.3, AMBER), Threshold(0.5, RED)],
    )
    layout.stat(
        "Replicas up",
        "Number of instances of the service that Prometheus scraped successfully.",
        [Query(f"count(up{{{SVC}}} == 1)")],
        thresholds=[Threshold(None, RED), Threshold(1, GREEN)],
    )
    layout.stat(
        "Restarts (10m)",
        "Process restarts in the last 10 minutes, summed over instances.",
        [Query(f"sum(changes(process_start_time_seconds{{{SVC}}}[10m]))")],
        thresholds=[Threshold(None, GREEN), Threshold(1, AMBER), Threshold(2, RED)],
    )
    layout.stat(
        "Version",
        "Release version reported by the running instances (service_build_info).",
        [Query(f"count by (version) (service_build_info{{{SVC}}})", "{{version}}", instant=True)],
        text_mode="name",
    )

    layout.row("Traffic and errors")
    layout.timeseries(
        "Request rate by status",
        "API requests per second, stacked by HTTP status code.",
        [Query(f"sum by (status) (rate(http_requests_total{{{SVC}, {API}}}[1m]))", "{{status}}")],
        unit="reqps",
        minimum=0,
        stack=True,
    )
    layout.timeseries(
        "Error ratio",
        "Share of API requests answered with 5xx. The line marks a 99.5% availability objective.",
        [
            Query(
                f'sum(rate(http_requests_total{{{SVC}, {API}, status=~"5.."}}[1m])) '
                f"/ sum(rate(http_requests_total{{{SVC}, {API}}}[1m]))",
                "5xx ratio",
            )
        ],
        unit="percentunit",
        minimum=0,
        thresholds=[Threshold(None, GREEN), Threshold(0.005, RED)],
    )
    layout.timeseries(
        "Request rate by route",
        "API requests per second for each route template.",
        [
            Query(
                f"sum by (method, route) (rate(http_requests_total{{{SVC}, {API}}}[1m]))",
                "{{method}} {{route}}",
            )
        ],
        unit="reqps",
        minimum=0,
    )

    layout.row("Latency")
    layout.timeseries(
        "Latency percentiles",
        "p50, p95 and p99 API request duration. Lines mark the 300 ms and 500 ms SLO thresholds.",
        [
            Query(_quantile(0.5, f"{SVC}, {API}"), "p50"),
            Query(_quantile(0.95, f"{SVC}, {API}"), "p95"),
            Query(_quantile(0.99, f"{SVC}, {API}"), "p99"),
        ],
        unit="s",
        minimum=0,
        width=12,
        thresholds=[Threshold(None, GREEN), Threshold(0.3, AMBER), Threshold(0.5, RED)],
    )
    layout.timeseries(
        "Requests in flight",
        "Requests being handled right now, per instance.",
        [Query(f"http_requests_in_flight{{{SVC}}}", "{{instance}}")],
        minimum=0,
        width=12,
    )

    layout.row("Saturation and resources")
    layout.timeseries(
        "Resident memory",
        "Process RSS per instance. Demo containers are limited to 192 MiB.",
        [Query(f"process_resident_memory_bytes{{{SVC}}}", "{{instance}}")],
        unit="bytes",
        minimum=0,
    )
    layout.timeseries(
        "CPU usage",
        "CPU cores used per instance.",
        [Query(f"rate(process_cpu_seconds_total{{{SVC}}}[1m])", "{{instance}}")],
        unit="short",
        minimum=0,
    )
    layout.timeseries(
        "Restarts",
        "Process restarts per instance over a sliding 10-minute window.",
        [Query(f"changes(process_start_time_seconds{{{SVC}}}[10m])", "{{instance}}")],
        minimum=0,
    )

    layout.row("Orders capacity (orders only)")
    layout.timeseries(
        "Worker utilization",
        "Time-weighted share of busy worker slots per orders replica. Above 0.9 is saturated.",
        [Query("orders_worker_utilization", "{{instance}}")],
        unit="percentunit",
        minimum=0,
        maximum=1,
        thresholds=[Threshold(None, GREEN), Threshold(0.9, RED)],
    )
    layout.timeseries(
        "Queue wait p95",
        "95th percentile time a request waited for a worker slot.",
        [
            Query(
                "histogram_quantile(0.95, sum by (le) "
                "(rate(orders_queue_wait_seconds_bucket[1m])))",
                "p95 wait",
            )
        ],
        unit="s",
        minimum=0,
    )
    layout.timeseries(
        "Database pool",
        "Postgres connections checked out versus open, summed over replicas.",
        [
            Query("sum(orders_db_pool_in_use)", "in use"),
            Query("sum(orders_db_pool_size)", "pool size"),
        ],
        minimum=0,
    )

    layout.row("Client view (loadgen)")
    layout.timeseries(
        "Loadgen requests by outcome",
        "What the synthetic clients observed: a black-box cross-check of the server-side SLIs.",
        [Query("sum by (outcome) (rate(loadgen_requests_total[1m]))", "{{outcome}}")],
        unit="reqps",
        minimum=0,
        width=12,
        stack=True,
    )
    layout.timeseries(
        "Loadgen latency percentiles",
        "Client-observed p50, p95 and p99 request duration.",
        [
            Query(
                f"histogram_quantile({q}, sum by (le) "
                "(rate(loadgen_request_duration_seconds_bucket[1m])))",
                label,
            )
            for q, label in ((0.5, "p50"), (0.95, "p95"), (0.99, "p99"))
        ],
        unit="s",
        minimum=0,
        width=12,
    )

    return dashboard(
        "relialoop-golden-signals",
        "Service Golden Signals",
        "Latency, traffic, errors and saturation for one service.",
        layout,
        variables=[custom_variable("service", "Service", ["gateway", "orders"])],
    )


SLO_NAMES = ["gateway-availability", "gateway-latency", "orders-availability", "orders-latency"]
SLO = 'slo="$slo"'


def slo_error_budget() -> dict[str, Any]:
    """Dashboard 2: SLI, error budget and burn rates for one SLO, plus an overview of all."""
    layout = Layout()

    layout.row("Selected SLO")
    layout.stat(
        "SLI (5m)",
        "Share of good events over the last 5 minutes.",
        [Query(f"1 - slo:sli_error:ratio_rate5m{{{SLO}}}")],
        unit="percentunit",
        decimals=2,
    )
    layout.stat(
        "Objective",
        "Target share of good events defined in the SLO spec.",
        [Query(f"slo:objective:ratio{{{SLO}}}")],
        unit="percentunit",
        decimals=1,
    )
    layout.stat(
        "Error budget remaining",
        "1 = untouched, 0 = spent, negative = overspent. Accounted over 1 hour in the demo "
        "profile (30 days in production).",
        [Query(f"slo:error_budget_remaining:ratio{{{SLO}}}")],
        unit="percentunit",
        decimals=1,
        thresholds=[Threshold(None, RED), Threshold(0.25, AMBER), Threshold(0.5, GREEN)],
    )
    layout.stat(
        "Burn rate (2m)",
        "How many times faster than sustainable the budget is being spent. "
        "Above 14.4 on both fast windows pages.",
        [Query(f"slo:burn_rate:ratio_2m{{{SLO}}}")],
        decimals=1,
        thresholds=[Threshold(None, GREEN), Threshold(1, AMBER), Threshold(14.4, RED)],
    )
    layout.stat(
        "Traffic (2m)",
        "Valid events per second. Alerts need at least the min_traffic_rps of the spec.",
        [Query(f"slo:sli_total:rate2m{{{SLO}}}")],
        unit="reqps",
        decimals=1,
    )
    layout.stat(
        "Firing burn alerts",
        "Number of burn-rate alerts currently firing for this SLO.",
        [Query(f'count(ALERTS{{{SLO}, alertstate="firing"}}) or vector(0)')],
        thresholds=[Threshold(None, GREEN), Threshold(1, RED)],
    )

    layout.row("SLI and budget")
    layout.timeseries(
        "SLI vs objective",
        "Share of good events over 5 minutes against the objective.",
        [
            Query(f"1 - slo:sli_error:ratio_rate5m{{{SLO}}}", "SLI (5m)"),
            Query(f"slo:objective:ratio{{{SLO}}}", "objective"),
        ],
        unit="percentunit",
        maximum=1,
        width=12,
    )
    layout.timeseries(
        "Error budget remaining",
        "Budget left in the accounting window. It recovers as bad minutes leave the window.",
        [Query(f"slo:error_budget_remaining:ratio{{{SLO}}}", "budget remaining")],
        unit="percentunit",
        maximum=1,
        width=12,
        thresholds=[Threshold(None, RED), Threshold(0.25, GREEN)],
    )

    layout.row("Burn rates")
    layout.timeseries(
        "Fast and slow burn (page)",
        "Fast: 2m and 30s windows against 14.4. Slow: 6m and 1m windows against 6. "
        "An alert needs both of its windows above the factor.",
        [
            Query(f"slo:burn_rate:ratio_2m{{{SLO}}}", "2m (fast, long)"),
            Query(f"slo:burn_rate:ratio_30s{{{SLO}}}", "30s (fast, short)"),
            Query(f"slo:burn_rate:ratio_6m{{{SLO}}}", "6m (slow, long)"),
            Query(f"slo:burn_rate:ratio_1m{{{SLO}}}", "1m (slow, short)"),
        ],
        minimum=0,
        width=12,
        thresholds=[Threshold(None, GREEN), Threshold(6, AMBER), Threshold(14.4, RED)],
    )
    layout.timeseries(
        "Ticket and trickle burn (ticket)",
        "Ticket: 15m and 3m windows against 3. Trickle: 30m and 5m windows against 1.",
        [
            Query(f"slo:burn_rate:ratio_15m{{{SLO}}}", "15m (ticket, long)"),
            Query(f"slo:burn_rate:ratio_3m{{{SLO}}}", "3m (ticket, short)"),
            Query(f"slo:burn_rate:ratio_30m{{{SLO}}}", "30m (trickle, long)"),
            Query(f"slo:burn_rate:ratio_5m{{{SLO}}}", "5m (trickle, short)"),
        ],
        minimum=0,
        width=12,
        thresholds=[Threshold(None, GREEN), Threshold(1, AMBER), Threshold(3, RED)],
    )

    layout.row("All SLOs")
    layout.timeseries(
        "Error budget remaining by SLO",
        "Budget left for every SLO.",
        [Query("slo:error_budget_remaining:ratio", "{{slo}}")],
        unit="percentunit",
        maximum=1,
        width=12,
    )
    layout.timeseries(
        "Fast-window burn rate by SLO",
        "2-minute burn rate for every SLO; the line marks the 14.4 page threshold.",
        [Query("slo:burn_rate:ratio_2m", "{{slo}}")],
        minimum=0,
        width=12,
        thresholds=[Threshold(None, GREEN), Threshold(14.4, RED)],
    )
    layout.table(
        "Firing SLO alerts",
        "Burn-rate alerts firing right now, with severity and tier.",
        [Query('ALERTS{alertname=~"SLOBurnRate.*", alertstate="firing"}', instant=True)],
        width=24,
    )

    return dashboard(
        "relialoop-slo",
        "SLO and Error Budget",
        "SLIs against objectives, error budget and multi-window burn rates.",
        layout,
        variables=[custom_variable("slo", "SLO", SLO_NAMES)],
    )


def pipeline_health() -> dict[str, Any]:
    """Dashboard 4: is the monitoring and alerting pipeline itself working?"""
    layout = Layout()

    layout.row("Heartbeat")
    layout.stat(
        "Watchdog",
        "1 while the always-firing Watchdog alert is being evaluated. 0 means rule "
        "evaluation is broken.",
        [Query('count(ALERTS{alertname="Watchdog", alertstate="firing"}) or vector(0)')],
        thresholds=[Threshold(None, RED), Threshold(1, GREEN)],
    )
    layout.stat(
        "Targets up",
        "Scrape targets that answered their last scrape.",
        [Query("count(up == 1)")],
        thresholds=[Threshold(None, RED), Threshold(1, GREEN)],
    )
    layout.stat(
        "Targets down",
        "Scrape targets that failed their last scrape.",
        [Query("count(up == 0) or vector(0)")],
        thresholds=[Threshold(None, GREEN), Threshold(1, RED)],
    )
    layout.stat(
        "Firing pages",
        "Page-severity alerts firing right now.",
        [Query('count(ALERTS{severity="page", alertstate="firing"}) or vector(0)')],
        thresholds=[Threshold(None, GREEN), Threshold(1, RED)],
    )
    layout.stat(
        "Firing tickets",
        "Ticket-severity alerts firing right now.",
        [Query('count(ALERTS{severity="ticket", alertstate="firing"}) or vector(0)')],
        thresholds=[Threshold(None, GREEN), Threshold(1, AMBER)],
    )
    layout.stat(
        "Failed notifications (5m)",
        "Notifications Alertmanager could not deliver in the last 5 minutes.",
        [Query("sum(increase(alertmanager_notifications_failed_total[5m])) or vector(0)")],
        decimals=0,
        thresholds=[Threshold(None, GREEN), Threshold(1, RED)],
    )

    layout.row("Scraping")
    layout.timeseries(
        "Target health",
        "1 = last scrape succeeded, 0 = failed, per target.",
        [Query("up", "{{service}} {{instance}}")],
        minimum=0,
        maximum=1,
        width=12,
    )
    layout.timeseries(
        "Scrape duration",
        "Time each scrape took. The scrape timeout is 4 s.",
        [Query("scrape_duration_seconds", "{{service}} {{instance}}")],
        unit="s",
        minimum=0,
        width=12,
    )

    layout.row("Rule evaluation")
    layout.timeseries(
        "Rule group evaluation time",
        "How long each rule group took to evaluate. Must stay well below the 5 s interval.",
        [Query("prometheus_rule_group_last_duration_seconds", "{{rule_group}}")],
        unit="s",
        minimum=0,
        width=12,
    )
    layout.timeseries(
        "Rule evaluation failures",
        "Failed rule evaluations per second. Anything above zero means an alert may be blind.",
        [Query("sum(rate(prometheus_rule_evaluation_failures_total[1m]))", "failures/s")],
        minimum=0,
        width=12,
    )

    layout.row("Alert delivery")
    layout.timeseries(
        "Alerts by state",
        "Alerts held by Alertmanager, by state.",
        [Query("alertmanager_alerts", "{{state}}")],
        minimum=0,
    )
    layout.timeseries(
        "Notifications sent",
        "Notifications per second by integration (the agent receives webhooks).",
        [
            Query(
                "sum by (integration) (rate(alertmanager_notifications_total[1m]))",
                "{{integration}}",
            )
        ],
        minimum=0,
    )
    layout.timeseries(
        "Notifications failed",
        "Failed notifications per second by integration. Triggers "
        "AlertmanagerNotificationsFailing.",
        [
            Query(
                "sum by (integration) (rate(alertmanager_notifications_failed_total[1m]))",
                "{{integration}}",
            )
        ],
        minimum=0,
    )
    layout.timeseries(
        "Firing alerts by name",
        "Alerts firing in Prometheus, by alert name.",
        [Query('count by (alertname) (ALERTS{alertstate="firing"})', "{{alertname}}")],
        minimum=0,
        width=12,
    )
    layout.timeseries(
        "Config reloads",
        "1 = the last configuration reload succeeded.",
        [
            Query("prometheus_config_last_reload_successful", "prometheus"),
            Query("alertmanager_config_last_reload_successful", "alertmanager"),
        ],
        minimum=0,
        maximum=1,
        width=12,
    )

    return dashboard(
        "relialoop-pipeline",
        "Pipeline Health",
        "Health of scraping, rule evaluation and alert delivery.",
        layout,
    )


DASHBOARDS: dict[str, Callable[[], dict[str, Any]]] = {
    "golden-signals.json": golden_signals,
    "slo-error-budget.json": slo_error_budget,
    "pipeline-health.json": pipeline_health,
}
