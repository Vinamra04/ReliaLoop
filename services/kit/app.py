"""Application factory wiring logging, metrics, health, chaos and release profile."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import Depends, FastAPI

from services.kit.chaos import ChaosState, chaos_gate, chaos_router, leak_loop
from services.kit.health import ReadinessCheck, always_ready, health_router
from services.kit.logging import RequestLogMiddleware, configure_logging, get_logger
from services.kit.metrics import HttpMetrics, MetricsMiddleware
from services.kit.release import bad_release_gate, schedule_crash
from services.kit.settings import ServiceSettings

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]
MIN_CHAOS_TOKEN_LENGTH = 16


@asynccontextmanager
async def _no_lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield


def create_app(
    settings: ServiceSettings,
    *,
    readiness: ReadinessCheck = always_ready,
    lifespan: Lifespan = _no_lifespan,
    enable_chaos: bool = True,
) -> FastAPI:
    """Create a FastAPI app with the standard operational surface.

    The service-specific `lifespan` runs inside the kit's own lifespan, which
    owns the leak task and the crash-profile timer.
    """
    configure_logging(settings.service_name, settings.log_level)
    if enable_chaos and len(settings.chaos_token) < MIN_CHAOS_TOKEN_LENGTH:
        raise ValueError("CHAOS_TOKEN must be set (run `python -m tools.cli init`)")

    metrics = HttpMetrics(settings.release_version, settings.release_profile)
    chaos = ChaosState(settings.chaos_default_duration_s, settings.chaos_max_duration_s)

    @asynccontextmanager
    async def kit_lifespan(app: FastAPI) -> AsyncIterator[None]:
        get_logger().info(
            "service_starting",
            version=settings.release_version,
            release_profile=settings.release_profile,
        )
        leak_task = asyncio.create_task(leak_loop(chaos)) if enable_chaos else None
        if settings.release_profile == "crash":
            schedule_crash(settings)
        try:
            async with lifespan(app):
                yield
        finally:
            if leak_task:
                leak_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await leak_task
            get_logger().info("service_stopped")

    dependencies = []
    if enable_chaos:
        dependencies.append(Depends(chaos_gate(chaos)))
    if settings.release_profile == "bad":
        dependencies.append(Depends(bad_release_gate(settings)))

    app = FastAPI(
        title=settings.service_name,
        version=settings.release_version,
        lifespan=kit_lifespan,
        dependencies=dependencies,
    )
    app.state.settings = settings
    app.state.metrics = metrics
    app.state.chaos = chaos

    # Last added is outermost: logging wraps metrics wraps the app.
    app.add_middleware(MetricsMiddleware, metrics=metrics)
    app.add_middleware(RequestLogMiddleware)

    app.include_router(health_router(metrics.registry, readiness))
    if enable_chaos:
        app.include_router(chaos_router(chaos, settings.chaos_token))
    return app


def serve(app: FastAPI, port: int) -> None:
    """Run the app under uvicorn with our JSON logging instead of uvicorn's."""
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 - containers must listen on all interfaces
        port=port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=10,
    )
