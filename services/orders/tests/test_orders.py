import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from services.orders.capacity import Overloaded, UtilizationTracker, WorkerPool
from services.orders.main import build_app
from services.orders.models import Order, OrderCreate
from services.orders.settings import OrdersSettings

TOKEN = "test-chaos-token-0123456789"
ORDER = {"customer": "ada", "items": [{"sku": "A-1", "qty": 2, "unit_price_cents": 250}]}


class InMemoryRepository:
    def __init__(self) -> None:
        self.orders: dict[UUID, Order] = {}
        self.fail_ping = False

    async def create(self, order: OrderCreate) -> Order:
        created = Order(
            id=uuid4(),
            customer=order.customer,
            items=order.items,
            total_cents=order.total_cents(),
            created_at=datetime.now(UTC),
        )
        self.orders[created.id] = created
        return created

    async def get(self, order_id: UUID) -> Order | None:
        return self.orders.get(order_id)

    async def list(self, limit: int, offset: int) -> list[Order]:
        return list(self.orders.values())[offset : offset + limit]

    async def ping(self) -> None:
        if self.fail_ping:
            raise ConnectionError("down")

    def pool_in_use(self) -> int:
        return 1

    def pool_size(self) -> int:
        return 4

    async def close(self) -> None:
        return None


def make_app(repo: InMemoryRepository | None = None, **overrides):
    repo = repo or InMemoryRepository()
    settings = OrdersSettings(chaos_token=TOKEN, orders_work_ms=0, **overrides)

    async def factory(_: OrdersSettings) -> InMemoryRepository:
        return repo

    return build_app(settings, factory)


def test_create_get_and_list_order():
    with TestClient(make_app()) as client:
        created = client.post("/orders", json=ORDER)
        assert created.status_code == 201
        body = created.json()
        assert body["total_cents"] == 500
        assert client.get(f"/orders/{body['id']}").json()["customer"] == "ada"
        listing = client.get("/orders?limit=5").json()
        assert listing["limit"] == 5
        assert [o["id"] for o in listing["orders"]] == [body["id"]]


def test_unknown_order_is_404_and_bad_input_is_422():
    with TestClient(make_app()) as client:
        assert client.get(f"/orders/{uuid4()}").status_code == 404
        assert client.get("/orders/not-a-uuid").status_code == 422
        assert client.post("/orders", json={"customer": "x", "items": []}).status_code == 422
        assert client.get("/orders?limit=1000").status_code == 422


def test_readyz_reflects_database_health():
    repo = InMemoryRepository()
    with TestClient(make_app(repo)) as client:
        assert client.get("/readyz").status_code == 200
        repo.fail_ping = True
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["failures"] == {"postgres": "ConnectionError"}


def test_orders_metrics_are_exposed():
    with TestClient(make_app()) as client:
        client.post("/orders", json=ORDER)
        body = client.get("/metrics").text
    assert "orders_worker_utilization" in body
    assert "orders_queue_wait_seconds_count 1.0" in body
    assert "orders_db_pool_in_use 1.0" in body
    assert "orders_db_pool_size 4.0" in body


async def test_overload_sheds_requests_with_503():
    app = make_app(orders_workers=1, orders_queue_timeout_s=0.05)
    app.state.settings.orders_work_ms = 300
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://orders") as client,
    ):
        responses = await asyncio.gather(*(client.get("/orders") for _ in range(3)))
    assert sorted(r.status_code for r in responses) == [200, 503, 503]


async def test_worker_pool_limits_concurrency_and_times_out():
    waits: list[float] = []
    pool = WorkerPool(1, 0.02, UtilizationTracker(1, 5.0), waits.append)
    async with pool.slot():
        with pytest.raises(Overloaded):
            async with pool.slot():
                pass
    async with pool.slot():
        pass
    assert len(waits) == 3


def test_utilization_is_time_weighted():
    now = [0.0]
    tracker = UtilizationTracker(workers=4, window_s=5.0, clock=lambda: now[0])
    tracker.acquire()
    tracker.acquire()
    now[0] = 2.0
    assert tracker.sample() == pytest.approx(0.5)
    tracker.release()
    tracker.release()
    now[0] = 4.0
    assert tracker.sample() == pytest.approx(0.25)
    now[0] = 20.0
    tracker.sample()
    now[0] = 21.0
    assert tracker.sample() == pytest.approx(0.0)


def test_utilization_saturates_at_one():
    now = [0.0]
    tracker = UtilizationTracker(workers=2, window_s=5.0, clock=lambda: now[0])
    tracker.acquire()
    tracker.acquire()
    now[0] = 3.0
    assert tracker.sample() == pytest.approx(1.0)
