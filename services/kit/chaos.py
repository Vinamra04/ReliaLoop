"""Token-protected fault injection for chaos testing.

Faults live in memory and auto-expire, so a forgotten experiment heals
itself. They are applied by a FastAPI dependency (after routing), which means
injected errors and latency are recorded against the real route template and
therefore show up in the SLIs exactly as genuine failures would.
"""

from __future__ import annotations

import asyncio
import os
import random
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from services.kit.logging import get_logger
from services.kit.routes import is_operational, route_template

MEBIBYTE = 1024 * 1024
LEAK_TICK_S = 1.0


class ErrorFaultRequest(BaseModel):
    """Fail a share of requests with the given status."""

    rate: float = Field(default=0.5, ge=0, le=1)
    status: int = Field(default=500, ge=400, le=599)
    message: str = Field(default="internal error: request processing failed", max_length=500)
    duration_s: int | None = Field(default=None, gt=0)


class LatencyFaultRequest(BaseModel):
    """Delay every request."""

    ms: int = Field(default=800, ge=0, le=30_000)
    jitter_ms: int = Field(default=200, ge=0, le=30_000)
    duration_s: int | None = Field(default=None, gt=0)


class LeakFaultRequest(BaseModel):
    """Allocate memory steadily, like a leak."""

    mb_per_s: float = Field(default=2.0, gt=0, le=64)
    max_mb: int = Field(default=600, gt=0, le=4096)
    duration_s: int | None = Field(default=None, gt=0)


class CrashFaultRequest(BaseModel):
    """Exit the process once, after a delay."""

    after_s: float = Field(default=5.0, ge=0, le=300)


@dataclass
class _ErrorFault:
    rate: float
    status: int
    message: str
    expires_at: float


@dataclass
class _LatencyFault:
    ms: int
    jitter_ms: int
    expires_at: float


@dataclass
class _LeakFault:
    mb_per_s: float
    max_mb: int
    expires_at: float


class ChaosState:
    """Active faults for one process. Clock and RNG are injectable for tests."""

    def __init__(
        self,
        default_duration_s: int,
        max_duration_s: int,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self._default_duration_s = default_duration_s
        self._max_duration_s = max_duration_s
        self._clock = clock
        self._rng = rng or random.Random()  # noqa: S311 - fault sampling, not security
        self._errors: _ErrorFault | None = None
        self._latency: _LatencyFault | None = None
        self._leak: _LeakFault | None = None
        self._ballast: list[bytes] = []

    def _expiry(self, duration_s: int | None) -> float:
        duration = min(duration_s or self._default_duration_s, self._max_duration_s)
        return self._clock() + duration

    def set_errors(self, req: ErrorFaultRequest) -> None:
        self._errors = _ErrorFault(req.rate, req.status, req.message, self._expiry(req.duration_s))

    def set_latency(self, req: LatencyFaultRequest) -> None:
        self._latency = _LatencyFault(req.ms, req.jitter_ms, self._expiry(req.duration_s))

    def set_leak(self, req: LeakFaultRequest) -> None:
        self._leak = _LeakFault(req.mb_per_s, req.max_mb, self._expiry(req.duration_s))

    def clear(self) -> None:
        """Remove every fault and release leaked memory."""
        self._errors = None
        self._latency = None
        self._leak = None
        self._ballast.clear()

    def _expire(self) -> None:
        now = self._clock()
        if self._errors and now >= self._errors.expires_at:
            self._errors = None
        if self._latency and now >= self._latency.expires_at:
            self._latency = None
        if self._leak and now >= self._leak.expires_at:
            # Like a real leak, already-allocated memory is not given back when
            # growth stops; only DELETE /chaos or a restart releases it.
            self._leak = None

    @property
    def leaked_mb(self) -> float:
        return sum(len(chunk) for chunk in self._ballast) / MEBIBYTE

    def active(self) -> dict[str, Any]:
        """Describe active faults with their remaining lifetime."""
        self._expire()
        now = self._clock()
        out: dict[str, Any] = {}
        if self._errors:
            out["errors"] = {
                "rate": self._errors.rate,
                "status": self._errors.status,
                "message": self._errors.message,
                "remaining_s": round(self._errors.expires_at - now, 1),
            }
        if self._latency:
            out["latency"] = {
                "ms": self._latency.ms,
                "jitter_ms": self._latency.jitter_ms,
                "remaining_s": round(self._latency.expires_at - now, 1),
            }
        if self._leak:
            out["leak"] = {
                "mb_per_s": self._leak.mb_per_s,
                "max_mb": self._leak.max_mb,
                "remaining_s": round(self._leak.expires_at - now, 1),
            }
        if self._ballast:
            out["leaked_mb"] = round(self.leaked_mb, 1)
        return out

    def latency_seconds(self) -> float:
        """Delay to apply to the current request (0 when no latency fault)."""
        self._expire()
        if not self._latency:
            return 0.0
        jitter = self._rng.uniform(-self._latency.jitter_ms, self._latency.jitter_ms)
        return max(0.0, (self._latency.ms + jitter) / 1000)

    def error_to_raise(self) -> tuple[int, str] | None:
        """Return (status, message) if this request should fail."""
        self._expire()
        if self._errors and self._rng.random() < self._errors.rate:
            return self._errors.status, self._errors.message
        return None

    def leak_tick(self, elapsed_s: float) -> None:
        """Allocate the memory due for `elapsed_s` seconds of leaking."""
        self._expire()
        if not self._leak or self.leaked_mb >= self._leak.max_mb:
            return
        remaining_mb = self._leak.max_mb - self.leaked_mb
        size = int(min(self._leak.mb_per_s * elapsed_s, remaining_mb) * MEBIBYTE)
        # Non-zero bytes so the pages are really resident, not lazily mapped.
        self._ballast.append(b"\x01" * size)


async def leak_loop(state: ChaosState) -> None:
    """Background task driving the leak fault once per tick."""
    while True:
        await asyncio.sleep(LEAK_TICK_S)
        state.leak_tick(LEAK_TICK_S)


def chaos_gate(state: ChaosState) -> Callable[[Request], Any]:
    """Build the app-level dependency that applies latency and error faults."""

    async def gate(request: Request) -> None:
        if is_operational(route_template(request.scope)):
            return
        delay = state.latency_seconds()
        if delay:
            await asyncio.sleep(delay)
        failure = state.error_to_raise()
        if failure:
            status, message = failure
            # The message is logged verbatim: it stands in for attacker-influenced
            # error text and is treated as untrusted data by the agent.
            get_logger().error(message, kind="request_failed", status=status)
            raise HTTPException(status_code=status, detail="request failed")

    return gate


def require_chaos_token(expected: str) -> Callable[[str | None], None]:
    """Build the dependency that authenticates chaos calls."""

    def check(x_chaos_token: str | None = Header(default=None)) -> None:
        if not x_chaos_token or not secrets.compare_digest(x_chaos_token, expected):
            raise HTTPException(status_code=401, detail="invalid chaos token")

    return check


def chaos_router(state: ChaosState, token: str) -> APIRouter:
    """Build the `/chaos` router. Every endpoint requires `X-Chaos-Token`."""
    router = APIRouter(
        prefix="/chaos", tags=["chaos"], dependencies=[Depends(require_chaos_token(token))]
    )

    @router.get("")
    async def show() -> dict[str, Any]:
        return {"active": state.active()}

    @router.delete("")
    async def clear() -> dict[str, Any]:
        state.clear()
        get_logger().warning("chaos_cleared")
        return {"active": state.active()}

    @router.post("/errors")
    async def errors(req: ErrorFaultRequest) -> dict[str, Any]:
        state.set_errors(req)
        get_logger().warning("chaos_fault_set", fault="errors", rate=req.rate, status=req.status)
        return {"active": state.active()}

    @router.post("/latency")
    async def latency(req: LatencyFaultRequest) -> dict[str, Any]:
        state.set_latency(req)
        get_logger().warning("chaos_fault_set", fault="latency", ms=req.ms)
        return {"active": state.active()}

    @router.post("/leak")
    async def leak(req: LeakFaultRequest) -> dict[str, Any]:
        state.set_leak(req)
        get_logger().warning("chaos_fault_set", fault="leak", mb_per_s=req.mb_per_s)
        return {"active": state.active()}

    @router.post("/crash")
    async def crash(req: CrashFaultRequest) -> dict[str, Any]:
        get_logger().warning("chaos_fault_set", fault="crash", after_s=req.after_s)
        asyncio.get_running_loop().call_later(req.after_s, os._exit, 1)
        return {"crash_in_s": req.after_s}

    return router
