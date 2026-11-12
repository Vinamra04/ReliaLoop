"""Order persistence behind a small interface so the API is testable without Postgres."""

from __future__ import annotations

import json
from typing import Any, Protocol
from uuid import UUID, uuid4

import asyncpg

from services.orders.models import Order, OrderCreate, OrderItem
from services.orders.settings import OrdersSettings


class OrderRepository(Protocol):
    """What the API needs from storage."""

    async def create(self, order: OrderCreate) -> Order: ...
    async def get(self, order_id: UUID) -> Order | None: ...
    async def list(self, limit: int, offset: int) -> list[Order]: ...
    async def ping(self) -> None: ...
    def pool_in_use(self) -> int: ...
    def pool_size(self) -> int: ...
    async def close(self) -> None: ...


def _to_order(row: Any) -> Order:
    return Order(
        id=row["id"],
        customer=row["customer"],
        items=[OrderItem(**item) for item in json.loads(row["items"])],
        total_cents=row["total_cents"],
        created_at=row["created_at"],
    )


class PostgresOrderRepository:
    """asyncpg-backed repository with an explicit per-query timeout."""

    def __init__(self, pool: asyncpg.Pool, timeout_s: float) -> None:
        self._pool = pool
        self._timeout_s = timeout_s

    @classmethod
    async def connect(cls, settings: OrdersSettings) -> PostgresOrderRepository:
        pool = await asyncpg.create_pool(
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.postgres_user,
            password=settings.postgres_password,
            database=settings.postgres_db,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            command_timeout=settings.db_timeout_s,
            timeout=settings.db_timeout_s,
        )
        return cls(pool, settings.db_timeout_s)

    async def create(self, order: OrderCreate) -> Order:
        row = await self._pool.fetchrow(
            "INSERT INTO orders (id, customer, items, total_cents) "
            "VALUES ($1, $2, $3::jsonb, $4) "
            "RETURNING id, customer, items, total_cents, created_at",
            uuid4(),
            order.customer,
            json.dumps([item.model_dump() for item in order.items]),
            order.total_cents(),
            timeout=self._timeout_s,
        )
        return _to_order(row)

    async def get(self, order_id: UUID) -> Order | None:
        row = await self._pool.fetchrow(
            "SELECT id, customer, items, total_cents, created_at FROM orders WHERE id = $1",
            order_id,
            timeout=self._timeout_s,
        )
        return _to_order(row) if row else None

    async def list(self, limit: int, offset: int) -> list[Order]:
        rows = await self._pool.fetch(
            "SELECT id, customer, items, total_cents, created_at FROM orders "
            "ORDER BY created_at DESC LIMIT $1 OFFSET $2",
            limit,
            offset,
            timeout=self._timeout_s,
        )
        return [_to_order(row) for row in rows]

    async def ping(self) -> None:
        await self._pool.fetchval("SELECT 1", timeout=self._timeout_s)

    def pool_in_use(self) -> int:
        return int(self._pool.get_size() - self._pool.get_idle_size())

    def pool_size(self) -> int:
        return int(self._pool.get_size())

    async def close(self) -> None:
        await self._pool.close()
