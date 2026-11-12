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


DASHBOARDS: dict[str, Callable[[], dict[str, Any]]] = {
    "golden-signals.json": golden_signals,
}
