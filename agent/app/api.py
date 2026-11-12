"""Agent HTTP API."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

from fastapi import Depends, FastAPI, Header, HTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from agent.app.config import AgentSettings
from agent.app.metrics import AgentMetrics
from agent.app.models import Accepted, AmWebhook
from services.kit.health import always_ready, health_router
from services.kit.logging import RequestLogMiddleware, configure_logging, get_logger
from services.kit.metrics import HttpMetrics, MetricsMiddleware

BEARER_PREFIX = "Bearer "

# Called with each validated notification; returns nothing. The pipeline
# plugs in here, keeping the HTTP layer free of incident logic.
WebhookHandler = Callable[[AmWebhook], Awaitable[None]]


async def _log_only(notification: AmWebhook) -> None:
    """Default handler: record that the notification arrived."""


class BodySizeLimitMiddleware:
    """Rejects requests whose declared body exceeds the limit (413) before reading them."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            declared = dict(scope.get("headers", [])).get(b"content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": b'{"detail":"body too large"}'})
                return
        await self.app(scope, receive, send)


def require_bearer(expected: str) -> Callable[[str | None], None]:
    """Build the dependency that authenticates Alertmanager and the deploy tool."""

    def check(authorization: str | None = Header(default=None)) -> None:
        token = (
            authorization[len(BEARER_PREFIX) :]
            if authorization and authorization.startswith(BEARER_PREFIX)
            else ""
        )
        if not token or not secrets.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    return check


def create_app(
    settings: AgentSettings,
    *,
    on_page: WebhookHandler = _log_only,
    on_ticket: WebhookHandler = _log_only,
) -> FastAPI:
    """Create the agent API."""
    configure_logging(settings.service_name, settings.log_level)
    http_metrics = HttpMetrics(settings.release_version, "good")
    metrics = AgentMetrics(http_metrics.registry)

    app = FastAPI(title="relialoop-agent", version=settings.release_version)
    app.state.settings = settings
    app.state.metrics = metrics
    app.add_middleware(MetricsMiddleware, metrics=http_metrics)
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.include_router(health_router(http_metrics.registry, always_ready))

    bearer = Depends(require_bearer(settings.webhook_token))

    async def receive(notification: AmWebhook, kind: str, handler: WebhookHandler) -> Accepted:
        if len(notification.alerts) > settings.max_alerts_per_webhook:
            raise HTTPException(status_code=413, detail="too many alerts in one notification")
        for alert in notification.alerts:
            metrics.alerts_received.labels(severity=alert.labels.get("severity", "none")).inc()
        real = [alert for alert in notification.alerts if not alert.is_test]
        # Alert names and counts only: labels and annotations may carry
        # attacker-influenced text and are not logged here.
        get_logger().info(
            "alert_notification_received",
            kind=kind,
            status=notification.status,
            group_key=notification.group_key,
            alerts=sorted({alert.name for alert in notification.alerts}),
            firing=sum(1 for alert in notification.alerts if alert.status == "firing"),
            resolved=sum(1 for alert in notification.alerts if alert.status == "resolved"),
            test_alerts=len(notification.alerts) - len(real),
        )
        if real:
            await handler(notification.model_copy(update={"alerts": real}))
        return Accepted(
            accepted=len(real), ignored_test_alerts=len(notification.alerts) - len(real)
        )

    @app.post("/webhook/alertmanager", status_code=202, dependencies=[bearer])
    async def webhook_page(notification: AmWebhook) -> Accepted:
        return await receive(notification, "page", on_page)

    @app.post("/webhook/ticket", status_code=202, dependencies=[bearer])
    async def webhook_ticket(notification: AmWebhook) -> Accepted:
        return await receive(notification, "ticket", on_ticket)

    return app
