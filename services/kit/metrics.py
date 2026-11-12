"""Golden-signal HTTP metrics for every service.

The metrics carry no `service` label: Prometheus attaches it by relabeling,
so the same code runs unchanged in every service.
"""

from __future__ import annotations

import time

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    PlatformCollector,
    ProcessCollector,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from services.kit.routes import route_template

# Buckets deliberately include the SLO thresholds 0.3 s and 0.5 s: a latency
# SLI can only be computed exactly at an existing bucket boundary.
LATENCY_BUCKETS = (0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 2.0, 5.0)


class HttpMetrics:
    """Owns one registry per application so tests never share global state."""

    def __init__(self, version: str, release_profile: str) -> None:
        self.registry = CollectorRegistry()
        ProcessCollector(registry=self.registry)
        PlatformCollector(registry=self.registry)
        self.requests = Counter(
            "http_requests_total",
            "HTTP requests by method, route template and status code.",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "http_request_duration_seconds",
            "HTTP request duration in seconds by method and route template.",
            ["method", "route"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.in_flight = Gauge(
            "http_requests_in_flight",
            "HTTP requests currently being handled.",
            registry=self.registry,
        )
        build_info = Gauge(
            "service_build_info",
            "Constant 1, labelled with the running release version and profile.",
            ["version", "release_profile"],
            registry=self.registry,
        )
        build_info.labels(version=version, release_profile=release_profile).set(1)


class MetricsMiddleware:
    """Pure ASGI middleware: records count, duration and in-flight for each request."""

    def __init__(self, app: ASGIApp, metrics: HttpMetrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status = 500  # an exception that escapes the app becomes a 500
        started = time.perf_counter()

        async def capture_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        self.metrics.in_flight.inc()
        try:
            await self.app(scope, receive, capture_status)
        finally:
            self.metrics.in_flight.dec()
            route = route_template(scope)
            method = scope["method"]
            self.metrics.requests.labels(method=method, route=route, status=str(status)).inc()
            self.metrics.duration.labels(method=method, route=route).observe(
                time.perf_counter() - started
            )
