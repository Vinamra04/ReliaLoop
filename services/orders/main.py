"""Orders API."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TypeVar
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query
from prometheus_client import Gauge, Histogram

from services.kit.app import create_app
from services.kit.logging import get_logger
from services.orders.capacity import Overloaded, UtilizationTracker, WorkerPool
from services.orders.models import Order, OrderCreate, OrderList
from services.orders.repository import OrderRepository, PostgresOrderRepository
from services.orders.settings import OrdersSettings

T = TypeVar("T")
RepositoryFactory = Callable[[OrdersSettings], Awaitable[OrderRepository]]
QUEUE_WAIT_BUCKETS = (0.005, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 1.5, 2.5)


async def _connect_postgres(settings: OrdersSettings) -> OrderRepository:
    return await PostgresOrderRepository.connect(settings)


def build_app(
    settings: OrdersSettings | None = None,
    repository_factory: RepositoryFactory = _connect_postgres,
) -> FastAPI:
    """Create the orders app; tests pass an in-memory repository factory."""
    settings = settings or OrdersSettings()
    state: dict[str, OrderRepository] = {}

    async def readiness() -> dict[str, str]:
        repo = state.get("repo")
        if repo is None:
            return {"postgres": "not connected"}
        try:
            await repo.ping()
        except Exception as exc:  # readiness must report, never raise
            return {"postgres": type(exc).__name__}
        return {}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        state["repo"] = await repository_factory(settings)
        sampler = asyncio.create_task(sample_utilization())
        try:
            yield
        finally:
            sampler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sampler
            await state["repo"].close()

    app = create_app(settings, readiness=readiness, lifespan=lifespan)
    registry = app.state.metrics.registry

    utilization = Gauge(
        "orders_worker_utilization",
        "Time-weighted share of busy worker slots over the last few seconds (0-1).",
        registry=registry,
    )
    queue_wait = Histogram(
        "orders_queue_wait_seconds",
        "Time a request waited for a worker slot.",
        buckets=QUEUE_WAIT_BUCKETS,
        registry=registry,
    )
    pool_in_use = Gauge(
        "orders_db_pool_in_use", "Database connections currently checked out.", registry=registry
    )
    pool_size = Gauge(
        "orders_db_pool_size", "Database connections currently open.", registry=registry
    )
    pool_in_use.set_function(lambda: state["repo"].pool_in_use() if "repo" in state else 0)
    pool_size.set_function(lambda: state["repo"].pool_size() if "repo" in state else 0)

    tracker = UtilizationTracker(settings.orders_workers, settings.utilization_window_s)
    workers = WorkerPool(
        settings.orders_workers, settings.orders_queue_timeout_s, tracker, queue_wait.observe
    )

    async def sample_utilization() -> None:
        while True:
            await asyncio.sleep(settings.utilization_sample_s)
            utilization.set(tracker.sample())

    async def handle(operation: Callable[[OrderRepository], Awaitable[T]]) -> T:
        """Run one unit of business work inside a worker slot."""
        try:
            async with workers.slot():
                await asyncio.sleep(settings.orders_work_ms / 1000)
                return await operation(state["repo"])
        except Overloaded:
            get_logger().warning("request shed: no worker slot available", kind="overloaded")
            raise HTTPException(status_code=503, detail="overloaded") from None

    @app.post("/orders", status_code=201)
    async def create_order(order: OrderCreate) -> Order:
        return await handle(lambda repo: repo.create(order))

    @app.get("/orders/{order_id}")
    async def get_order(order_id: UUID) -> Order:
        found = await handle(lambda repo: repo.get(order_id))
        if found is None:
            raise HTTPException(status_code=404, detail="order not found")
        return found

    @app.get("/orders")
    async def list_orders(
        limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0)
    ) -> OrderList:
        orders = await handle(lambda repo: repo.list(limit, offset))
        return OrderList(orders=orders, limit=limit, offset=offset)

    return app
