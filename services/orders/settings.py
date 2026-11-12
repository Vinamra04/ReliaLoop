"""Orders settings."""

from __future__ import annotations

from pydantic import Field

from services.kit.settings import ServiceSettings


class OrdersSettings(ServiceSettings):
    """Capacity is `orders_workers / (orders_work_ms / 1000)` requests per second per replica."""

    service_name: str = "orders"
    port: int = 8001

    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "relialoop"
    postgres_password: str = ""
    postgres_db: str = "orders"
    db_pool_min: int = Field(default=2, ge=1)
    db_pool_max: int = Field(default=8, ge=1)
    db_timeout_s: float = Field(default=2.0, gt=0)

    orders_workers: int = Field(default=4, ge=1)
    orders_work_ms: int = Field(default=40, ge=0)
    # A request that cannot get a worker slot in time is shed with 503 so the
    # queue (and memory) stays bounded under overload.
    orders_queue_timeout_s: float = Field(default=1.5, gt=0)
    utilization_window_s: float = Field(default=5.0, gt=0)
    utilization_sample_s: float = Field(default=1.0, gt=0)
