"""Gateway settings."""

from __future__ import annotations

from pydantic import Field

from services.kit.settings import ServiceSettings


class GatewaySettings(ServiceSettings):
    service_name: str = "gateway"
    port: int = 8000

    orders_host: str = "orders"
    orders_port: int = 8001
    # No retries on purpose: upstream failures must stay visible in the SLIs.
    upstream_timeout_s: float = Field(default=2.0, gt=0)
    upstream_connect_timeout_s: float = Field(default=1.0, gt=0)
    # Short TTL so newly scaled replicas receive traffic within seconds.
    dns_cache_ttl_s: float = Field(default=2.0, ge=0)
