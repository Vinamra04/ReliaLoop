"""Gateway API: proxies order requests to the orders replicas."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, Depends, FastAPI, Header, Query, Response
from fastapi.responses import JSONResponse

from services.gateway.discovery import NoReplicasError, ReplicaResolver
from services.gateway.settings import GatewaySettings
from services.kit.app import create_app
from services.kit.chaos import require_chaos_token
from services.kit.logging import REQUEST_ID_HEADER, current_request_id, get_logger

ChaosFault = Literal["errors", "latency", "leak", "crash"]
CHAOS_HEADER = "X-Chaos-Token"


def build_app(
    settings: GatewaySettings | None = None,
    resolver: ReplicaResolver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Create the gateway app; tests inject a resolver and a mock transport."""
    settings = settings or GatewaySettings()
    replicas = resolver or ReplicaResolver(
        settings.orders_host, settings.orders_port, settings.dns_cache_ttl_s
    )
    # Keep-alive is disabled so each request can land on a different replica;
    # a pooled connection would pin traffic to whichever replica it reached first.
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.upstream_timeout_s, connect=settings.upstream_connect_timeout_s
        ),
        limits=httpx.Limits(max_keepalive_connections=0),
        headers={"Connection": "close"},
        transport=transport,
    )

    async def readiness() -> dict[str, str]:
        try:
            response = await client.get(f"{await replicas.pick()}/readyz")
        except (httpx.HTTPError, NoReplicasError) as exc:
            return {"orders": type(exc).__name__}
        return {} if response.status_code == 200 else {"orders": f"status {response.status_code}"}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await client.aclose()

    app = create_app(settings, readiness=readiness, lifespan=lifespan)

    async def proxy(method: str, path: str, **kwargs: Any) -> Response:
        """Forward one request and map upstream failures to gateway statuses."""
        headers = {}
        request_id = current_request_id()
        if request_id:
            headers[REQUEST_ID_HEADER] = request_id
        log = get_logger()
        try:
            base = await replicas.pick()
            upstream = await client.request(method, f"{base}{path}", headers=headers, **kwargs)
        except httpx.TimeoutException:
            log.error("upstream timeout", kind="upstream_timeout", upstream="orders")
            return JSONResponse({"detail": "upstream timeout"}, status_code=504)
        except (httpx.HTTPError, NoReplicasError) as exc:
            log.error(
                "upstream unreachable",
                kind="upstream_unreachable",
                upstream="orders",
                error=type(exc).__name__,
            )
            return JSONResponse({"detail": "upstream unavailable"}, status_code=502)
        if upstream.status_code >= 500:
            log.error(
                "upstream error",
                kind="upstream_error",
                upstream="orders",
                upstream_status=upstream.status_code,
            )
            return JSONResponse({"detail": "upstream error"}, status_code=502)
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    @app.get("/api/orders")
    async def list_orders(
        limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0)
    ) -> Response:
        return await proxy("GET", "/orders", params={"limit": limit, "offset": offset})

    @app.get("/api/orders/{order_id}")
    async def get_order(order_id: UUID) -> Response:
        return await proxy("GET", f"/orders/{order_id}")

    @app.post("/api/orders")
    async def create_order(order: dict[str, Any] = Body(...)) -> Response:
        return await proxy("POST", "/orders", json=order)

    # Orders replicas are not published to the host, so chaos for orders is
    # fanned out through the gateway to every replica.
    fanout = APIRouter(
        prefix="/chaos/target/orders",
        tags=["chaos"],
        dependencies=[Depends(require_chaos_token(settings.chaos_token))],
    )

    async def fan_out(method: str, suffix: str, token: str, body: Any = None) -> JSONResponse:
        try:
            targets = await replicas.replicas()
        except NoReplicasError:
            return JSONResponse({"detail": "no orders replicas"}, status_code=502)

        async def call(base: str) -> dict[str, Any]:
            try:
                response = await client.request(
                    method, f"{base}/chaos{suffix}", json=body, headers={CHAOS_HEADER: token}
                )
            except httpx.HTTPError as exc:
                return {"replica": base, "error": type(exc).__name__}
            return {"replica": base, "status": response.status_code, "body": response.json()}

        results = await asyncio.gather(*(call(base) for base in targets))
        ok = all(result.get("status") == 200 for result in results)
        return JSONResponse({"replicas": results}, status_code=200 if ok else 502)

    @fanout.get("")
    async def orders_chaos_show(x_chaos_token: str = Header()) -> JSONResponse:
        return await fan_out("GET", "", x_chaos_token)

    @fanout.delete("")
    async def orders_chaos_clear(x_chaos_token: str = Header()) -> JSONResponse:
        return await fan_out("DELETE", "", x_chaos_token)

    @fanout.post("/{fault}")
    async def orders_chaos_set(
        fault: ChaosFault,
        body: dict[str, Any] = Body(default_factory=dict),
        x_chaos_token: str = Header(),
    ) -> JSONResponse:
        return await fan_out("POST", f"/{fault}", x_chaos_token, body)

    app.include_router(fanout)
    return app
