"""Loadgen control API and metrics."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from prometheus_client import Counter, Histogram
from pydantic import BaseModel, Field

from services.kit.app import create_app
from services.kit.logging import get_logger
from services.kit.metrics import LATENCY_BUCKETS
from services.loadgen.engine import LoadEngine, RateController
from services.loadgen.settings import LoadgenSettings

MIN_TOKEN_LENGTH = 16


class RateRequest(BaseModel):
    """`duration_s` omitted changes the base rate; given, it is a temporary surge."""

    rps: float = Field(ge=0)
    duration_s: float | None = Field(default=None, gt=0, le=3600)


def build_app(
    settings: LoadgenSettings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    autostart: bool = True,
) -> FastAPI:
    """Create the loadgen app; tests inject a transport and disable autostart."""
    settings = settings or LoadgenSettings()
    if len(settings.loadgen_token) < MIN_TOKEN_LENGTH:
        raise ValueError("LOADGEN_TOKEN must be set (run `python -m tools.cli init`)")

    rate = RateController(settings.loadgen_rps, settings.loadgen_max_rps)
    holder: dict[str, LoadEngine] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Unlike the gateway, the client keeps connections alive: it models
        # many users behind a few long-lived connections to the edge.
        async with httpx.AsyncClient(
            base_url=settings.target_url,
            timeout=settings.request_timeout_s,
            transport=transport,
        ) as client:
            engine = LoadEngine(settings, client, rate, record)
            holder["engine"] = engine
            app.state.engine = engine
            runner = asyncio.create_task(engine.run()) if autostart else None
            try:
                yield
            finally:
                if runner:
                    runner.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await runner

    app = create_app(settings, lifespan=lifespan, enable_chaos=False)
    registry = app.state.metrics.registry
    requests_total = Counter(
        "loadgen_requests_total",
        "Requests sent by the load generator, by client-observed outcome.",
        ["outcome"],
        registry=registry,
    )
    request_duration = Histogram(
        "loadgen_request_duration_seconds",
        "Client-observed request duration in seconds.",
        buckets=LATENCY_BUCKETS,
        registry=registry,
    )

    def record(outcome: str, duration_s: float) -> None:
        requests_total.labels(outcome=outcome).inc()
        if outcome != "dropped":
            request_duration.observe(duration_s)

    def require_token(x_loadgen_token: str | None = Header(default=None)) -> None:
        if not x_loadgen_token or not secrets.compare_digest(
            x_loadgen_token, settings.loadgen_token
        ):
            raise HTTPException(status_code=401, detail="invalid loadgen token")

    def status_body() -> dict[str, Any]:
        engine = holder.get("engine")
        return {**rate.status(), "in_flight": engine.in_flight if engine else 0}

    @app.get("/status")
    async def status() -> dict[str, Any]:
        return status_body()

    @app.post("/rate", dependencies=[Depends(require_token)])
    async def set_rate(req: RateRequest) -> dict[str, Any]:
        rate.set_rate(req.rps, req.duration_s)
        get_logger().info("rate_changed", rps=req.rps, duration_s=req.duration_s)
        return status_body()

    return app
