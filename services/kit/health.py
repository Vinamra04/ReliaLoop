"""Liveness, readiness and metrics endpoints."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

# Returns a map of dependency name -> error text; empty means ready.
ReadinessCheck = Callable[[], Awaitable[dict[str, str]]]


async def always_ready() -> dict[str, str]:
    """Readiness for services without dependencies."""
    return {}


def health_router(registry: CollectorRegistry, readiness: ReadinessCheck) -> APIRouter:
    """Build the operational router.

    `/healthz` never touches dependencies (a dependency outage must not get the
    container restarted); `/readyz` reports whether the service can do useful work.
    """
    router = APIRouter(tags=["ops"])

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/readyz")
    async def readyz() -> JSONResponse:
        failures = await readiness()
        if failures:
            return JSONResponse({"status": "not_ready", "failures": failures}, status_code=503)
        return JSONResponse({"status": "ready"})

    @router.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return router
