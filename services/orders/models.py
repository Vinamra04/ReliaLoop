"""Order domain models."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class OrderItem(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    qty: int = Field(ge=1, le=1000)
    unit_price_cents: int = Field(ge=0, le=10_000_000)


class OrderCreate(BaseModel):
    customer: str = Field(min_length=1, max_length=120)
    items: list[OrderItem] = Field(min_length=1, max_length=50)

    def total_cents(self) -> int:
        return sum(item.qty * item.unit_price_cents for item in self.items)


class Order(BaseModel):
    id: UUID
    customer: str
    items: list[OrderItem]
    total_cents: int
    created_at: datetime


class OrderList(BaseModel):
    orders: list[Order]
    limit: int
    offset: int
