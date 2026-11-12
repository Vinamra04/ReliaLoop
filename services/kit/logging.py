"""Structured JSON logging and the per-request log line.

Every log line carries `service` and, inside a request, `request_id`, so a
request can be followed across gateway and orders.
"""

from __future__ import annotations

import logging
import re
import sys
import time
import uuid
from typing import Any

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from services.kit.routes import QUIET_ROUTES, route_template

REQUEST_ID_HEADER = "x-request-id"
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


def configure_logging(service: str, level: str = "INFO") -> None:
    """Configure structlog to emit one JSON object per line on stdout."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(service=service)


def get_logger() -> Any:
    """Return the structlog logger (typed Any: structlog's proxy is dynamic)."""
    return structlog.get_logger()


def current_request_id() -> str | None:
    """Request id bound for the in-flight request, used to propagate it downstream."""
    value = structlog.contextvars.get_contextvars().get("request_id")
    return value if isinstance(value, str) else None


def _incoming_request_id(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name == REQUEST_ID_HEADER.encode():
            candidate: str = value.decode("latin-1")
            # Only accept well-formed ids: the value ends up in logs.
            if _SAFE_REQUEST_ID.match(candidate):
                return candidate
    return uuid.uuid4().hex


class RequestLogMiddleware:
    """Binds `request_id` and logs one line per request with the route template."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope)
        status = 500
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append(
                    (REQUEST_ID_HEADER.encode(), request_id.encode())
                )
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_request_id)
            finally:
                route = route_template(scope)
                log = get_logger()
                emit = log.debug if route in QUIET_ROUTES else log.info
                emit(
                    "request",
                    method=scope["method"],
                    route=route,
                    status=status,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
