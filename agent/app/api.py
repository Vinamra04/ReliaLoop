"""Agent HTTP API."""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from agent.adapters.sqlite_store import SqliteStore
from agent.app.config import AgentSettings
from agent.app.domain import DeployEvent, Incident
from agent.app.metrics import AgentMetrics
from agent.app.models import Accepted, AmWebhook, ApprovalIn, DeployEventIn, ModeIn
from agent.app.pipeline import ApprovalError, IncidentPipeline, QueueFullError
from agent.evidence.collector import utc_now
from services.kit.health import ReadinessCheck, health_router
from services.kit.logging import RequestLogMiddleware, configure_logging, get_logger
from services.kit.metrics import HttpMetrics, MetricsMiddleware

BEARER_PREFIX = "Bearer "
RUNBOOK_NAME = re.compile(r"^[a-z0-9-]{1,64}$")


@dataclass
class Components:
    """Everything the API needs, built once at startup."""

    store: SqliteStore
    pipeline: IncidentPipeline
    readiness: ReadinessCheck
    close: Callable[[], Awaitable[None]]


# Builds the components; production wiring and tests provide different ones.
ComponentFactory = Callable[[AgentSettings, AgentMetrics], Awaitable[Components]]


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


def require_approver(expected: str) -> Callable[[str | None], None]:
    """Build the dependency that authenticates human decisions and admin calls."""

    def check(x_approver_token: str | None = Header(default=None)) -> None:
        if not x_approver_token or not secrets.compare_digest(x_approver_token, expected):
            raise HTTPException(status_code=401, detail="invalid or missing approver token")

    return check


def incident_summary(incident: Incident) -> dict[str, Any]:
    return incident.model_dump(mode="json")


def create_app(settings: AgentSettings, factory: ComponentFactory) -> FastAPI:
    """Create the agent API. `factory` builds the pipeline and its dependencies."""
    configure_logging(settings.service_name, settings.log_level)
    http_metrics = HttpMetrics(settings.release_version, "good")
    metrics = AgentMetrics(http_metrics.registry)
    holder: dict[str, Components] = {}

    def parts() -> Components:
        return holder["components"]

    async def readiness() -> dict[str, str]:
        if "components" not in holder:
            return {"agent": "starting"}
        return await parts().readiness()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        components = await factory(settings, metrics)
        holder["components"] = components
        await components.pipeline.start()
        get_logger().info(
            "agent_started", mode=components.pipeline.mode, diagnosis_mode=settings.diagnosis_mode
        )
        try:
            yield
        finally:
            await components.pipeline.stop()
            await components.close()

    app = FastAPI(title="relialoop-agent", version=settings.release_version, lifespan=lifespan)
    app.state.settings = settings
    app.state.metrics = metrics
    app.state.holder = holder
    app.add_middleware(MetricsMiddleware, metrics=http_metrics)
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.include_router(health_router(http_metrics.registry, readiness))

    bearer = Depends(require_bearer(settings.webhook_token))
    approver = Depends(require_approver(settings.approver_token))

    async def db(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    # --- webhooks ---------------------------------------------------------------

    async def receive(notification: AmWebhook, kind: str) -> Accepted:
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
            cleaned = notification.model_copy(update={"alerts": real})
            try:
                if kind == "page":
                    await parts().pipeline.handle_page(cleaned)
                else:
                    await parts().pipeline.handle_ticket(cleaned)
            except QueueFullError:
                raise HTTPException(status_code=429, detail="incident queue is full") from None
        return Accepted(
            accepted=len(real), ignored_test_alerts=len(notification.alerts) - len(real)
        )

    @app.post("/webhook/alertmanager", status_code=202, dependencies=[bearer])
    async def webhook_page(notification: AmWebhook) -> Accepted:
        return await receive(notification, "page")

    @app.post("/webhook/ticket", status_code=202, dependencies=[bearer])
    async def webhook_ticket(notification: AmWebhook) -> Accepted:
        return await receive(notification, "ticket")

    @app.post("/events/deploy", status_code=202, dependencies=[bearer])
    async def deploy_event(event: DeployEventIn) -> dict[str, str]:
        await parts().pipeline.record_deploy(DeployEvent(**event.model_dump(), ts=utc_now()))
        return {"recorded": event.status}

    # --- read API (localhost only) -------------------------------------------------

    @app.get("/status")
    async def status() -> dict[str, Any]:
        pipeline = parts().pipeline
        return {
            "mode": pipeline.mode,
            "diagnosis_mode": pipeline.diagnosis_mode,
            "queue_depth": pipeline.queue_depth,
            "workers": settings.agent_workers,
            "version": settings.release_version,
        }

    @app.get("/incidents")
    async def list_incidents(
        limit: int = Query(default=50, ge=1, le=500),
        status: str | None = Query(default=None, pattern="^(open|closed)$"),
    ) -> dict[str, Any]:
        incidents = await db(parts().store.list_incidents, limit, status)
        return {"incidents": [incident_summary(incident) for incident in incidents]}

    @app.get("/incidents/{incident_id}")
    async def get_incident(incident_id: str) -> dict[str, Any]:
        store = parts().store
        incident = await db(store.get_incident, incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="incident not found")
        intact, rows = await db(store.verify_audit_chain)
        return {
            "incident": incident_summary(incident),
            "timeline": await db(store.events, incident_id),
            "alerts": await db(store.alerts, incident_id),
            "evidence": await db(store.evidence, incident_id),
            "diagnoses": await db(store.diagnoses, incident_id),
            "actions": await db(store.actions, incident_id),
            "audit": await db(store.audit_entries, incident_id),
            "audit_chain": {"intact": intact, "rows": rows},
        }

    @app.get("/actions/pending")
    async def pending_actions() -> dict[str, Any]:
        return {"actions": await db(parts().store.pending_actions)}

    @app.get("/deploys")
    async def deploys(
        service: str | None = Query(default=None, pattern=r"^[a-z][a-z0-9-]{0,62}$"),
        limit: int = Query(default=20, ge=1, le=200),
    ) -> dict[str, Any]:
        events = await db(parts().store.deploys, service, limit)
        return {"deploys": [event.model_dump(mode="json") for event in events]}

    @app.get("/tickets")
    async def tickets(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
        return {"tickets": await db(parts().store.tickets, limit)}

    @app.get("/runbooks/{name}", response_class=PlainTextResponse)
    async def runbook(name: str) -> PlainTextResponse:
        # The name is restricted to a slug, so it cannot escape the runbook directory.
        path = settings.runbooks_dir / f"{name}.md"
        if not RUNBOOK_NAME.match(name) or not path.is_file():
            raise HTTPException(status_code=404, detail="runbook not found")
        text = await asyncio.to_thread(path.read_text, "utf-8")
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

    # --- human decisions and admin -------------------------------------------------

    async def decide(
        incident_id: str, body: ApprovalIn, decision: Callable[..., Awaitable[Incident]]
    ) -> dict[str, Any]:
        try:
            incident = await decision(incident_id, body.actor)
        except ApprovalError as exc:
            status_code = 404 if "unknown incident" in str(exc) else 409
            raise HTTPException(status_code=status_code, detail=str(exc)) from None
        except QueueFullError:
            raise HTTPException(status_code=429, detail="incident queue is full") from None
        return {"incident": incident_summary(incident)}

    @app.post("/incidents/{incident_id}/approve", dependencies=[approver])
    async def approve(incident_id: str, body: ApprovalIn | None = None) -> dict[str, Any]:
        return await decide(incident_id, body or ApprovalIn(), parts().pipeline.approve)

    @app.post("/incidents/{incident_id}/reject", dependencies=[approver])
    async def reject(incident_id: str, body: ApprovalIn | None = None) -> dict[str, Any]:
        payload = body or ApprovalIn()

        async def do_reject(target: str, actor: str) -> Incident:
            return await parts().pipeline.reject(target, actor, payload.reason)

        return await decide(incident_id, payload, do_reject)

    @app.post("/admin/mode", dependencies=[approver])
    async def set_mode(body: ModeIn) -> dict[str, str]:
        pipeline = parts().pipeline
        if body.mode is None and body.diagnosis_mode is None:
            raise HTTPException(status_code=422, detail="give mode and/or diagnosis_mode")
        if body.diagnosis_mode is not None:
            try:
                await pipeline.set_diagnosis_mode(body.diagnosis_mode, body.actor)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from None
        if body.mode is not None:
            await pipeline.set_mode(body.mode, body.actor)
        get_logger().warning(
            "agent_mode_changed",
            mode=pipeline.mode,
            diagnosis_mode=pipeline.diagnosis_mode,
            actor=body.actor,
        )
        return {"mode": pipeline.mode, "diagnosis_mode": pipeline.diagnosis_mode}

    return app
