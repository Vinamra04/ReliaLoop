import random
from collections import Counter
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from services.gateway.discovery import NoReplicasError, ReplicaResolver
from services.gateway.main import build_app
from services.gateway.settings import GatewaySettings

TOKEN = "test-chaos-token-0123456789"
AUTH = {"X-Chaos-Token": TOKEN}
ORDER = {"customer": "ada", "items": [{"sku": "A-1", "qty": 1, "unit_price_cents": 100}]}


def resolver_for(addresses: list[str]) -> ReplicaResolver:
    async def lookup(host: str, port: int) -> list[str]:
        return addresses

    return ReplicaResolver("orders", 8001, ttl_s=60, lookup=lookup, rng=random.Random(3))


def make_client(handler, addresses: list[str] | None = None) -> TestClient:
    app = build_app(
        GatewaySettings(chaos_token=TOKEN),
        resolver=resolver_for(addresses or ["10.0.0.1"]),
        transport=httpx.MockTransport(handler),
    )
    return TestClient(app)


def test_proxies_requests_and_propagates_request_id():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"id": "x", **ORDER})
        return httpx.Response(200, json={"orders": []})

    with make_client(handler) as client:
        created = client.post("/api/orders", json=ORDER, headers={"X-Request-ID": "rid-9"})
        listed = client.get("/api/orders?limit=5&offset=10")
    assert created.status_code == 201
    assert created.json()["customer"] == "ada"
    assert listed.json() == {"orders": []}
    assert seen[0].url == "http://10.0.0.1:8001/orders"
    assert seen[0].headers["x-request-id"] == "rid-9"
    assert seen[0].headers["connection"] == "close"
    assert seen[1].url.params["limit"] == "5"
    assert seen[1].url.params["offset"] == "10"


def test_client_errors_pass_through():
    with make_client(lambda request: httpx.Response(404, json={"detail": "order not found"})) as c:
        response = c.get(f"/api/orders/{uuid4()}")
    assert response.status_code == 404
    assert response.json() == {"detail": "order not found"}


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (httpx.ReadTimeout("slow"), 504),
        (httpx.ConnectTimeout("slow"), 504),
        (httpx.ConnectError("refused"), 502),
        (httpx.RemoteProtocolError("reset"), 502),
    ],
)
def test_upstream_failures_are_mapped(failure: Exception, expected: int):
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    with make_client(handler) as client:
        assert client.get("/api/orders").status_code == expected


@pytest.mark.parametrize("upstream_status", [500, 503])
def test_upstream_5xx_becomes_502(upstream_status: int):
    with make_client(lambda request: httpx.Response(upstream_status, json={})) as client:
        assert client.get("/api/orders").status_code == 502


def test_no_replicas_is_502_and_not_ready():
    async def lookup(host: str, port: int) -> list[str]:
        raise OSError("name not known")

    app = build_app(
        GatewaySettings(chaos_token=TOKEN),
        resolver=ReplicaResolver("orders", 8001, ttl_s=0, lookup=lookup),
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )
    with TestClient(app) as client:
        assert client.get("/api/orders").status_code == 502
        ready = client.get("/readyz")
    assert ready.status_code == 503
    assert ready.json()["failures"] == {"orders": "NoReplicasError"}


def test_readyz_follows_orders_readiness():
    status = {"code": 200}
    with make_client(lambda request: httpx.Response(status["code"], json={})) as client:
        assert client.get("/readyz").status_code == 200
        status["code"] = 503
        assert client.get("/readyz").json()["failures"] == {"orders": "status 503"}


def test_load_is_spread_across_replicas():
    hits: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        hits[request.url.host] += 1
        return httpx.Response(200, json={"orders": []})

    with make_client(handler, ["10.0.0.1", "10.0.0.2", "10.0.0.3"]) as client:
        for _ in range(300):
            client.get("/api/orders")
    assert set(hits) == {"10.0.0.1", "10.0.0.2", "10.0.0.3"}
    assert min(hits.values()) > 60


def test_chaos_fans_out_to_every_orders_replica():
    calls: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), request.headers["x-chaos-token"]))
        return httpx.Response(200, json={"active": {}})

    with make_client(handler, ["10.0.0.1", "10.0.0.2"]) as client:
        assert client.post("/chaos/target/orders/errors", json={"rate": 0.5}).status_code == 401
        set_response = client.post("/chaos/target/orders/errors", json={"rate": 0.5}, headers=AUTH)
        assert client.delete("/chaos/target/orders", headers=AUTH).status_code == 200
        assert client.post("/chaos/target/orders/bogus", json={}, headers=AUTH).status_code == 422
    assert set_response.status_code == 200
    assert len(set_response.json()["replicas"]) == 2
    assert ("POST", "http://10.0.0.1:8001/chaos/errors", TOKEN) in calls
    assert ("POST", "http://10.0.0.2:8001/chaos/errors", TOKEN) in calls
    assert ("DELETE", "http://10.0.0.2:8001/chaos", TOKEN) in calls


async def test_resolver_caches_until_ttl_expires():
    now = [0.0]
    lookups = []

    async def lookup(host: str, port: int) -> list[str]:
        lookups.append(host)
        return ["10.0.0.1"] if len(lookups) == 1 else ["10.0.0.1", "10.0.0.2"]

    resolver = ReplicaResolver("orders", 8001, ttl_s=2, lookup=lookup, clock=lambda: now[0])
    assert await resolver.replicas() == ["http://10.0.0.1:8001"]
    now[0] = 1.0
    assert await resolver.replicas() == ["http://10.0.0.1:8001"]
    now[0] = 2.5
    assert len(await resolver.replicas()) == 2
    assert len(lookups) == 2


async def test_resolver_raises_when_name_has_no_addresses():
    async def lookup(host: str, port: int) -> list[str]:
        return []

    with pytest.raises(NoReplicasError):
        await ReplicaResolver("orders", 8001, ttl_s=2, lookup=lookup).pick()
