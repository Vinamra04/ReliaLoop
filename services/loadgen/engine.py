"""Open-loop traffic engine.

Open loop means the arrival rate does not slow down when the system under
test does: that is how real users behave during an incident and it is what
makes saturation visible. Arrivals follow a Poisson process (exponential
inter-arrival times), which provides natural think-time jitter.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import httpx

from services.loadgen.settings import LoadgenSettings

CUSTOMERS = ("ada", "grace", "linus", "margaret", "dennis", "barbara", "ken", "radia")
SKUS = ("KB-101", "MS-220", "MN-27Q", "HD-4TB", "CB-USBC", "WC-1080", "HS-PRO", "DK-TB4")
MAX_SLEEP_S = 0.5
# Never burst more than this much backlog after falling behind schedule.
CATCH_UP_LIMIT_S = 0.1

# Called with (outcome, duration_seconds) for every finished or dropped request.
Recorder = Callable[[str, float], None]


def classify(status: int) -> str:
    """Map a response status to an outcome label."""
    if status >= 500:
        return "error"
    if status >= 400:
        return "client_error"
    return "ok"


class RateController:
    """Base rate plus an optional temporary override (a surge)."""

    def __init__(
        self, base_rps: float, max_rps: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._base_rps = base_rps
        self._max_rps = max_rps
        self._clock = clock
        self._override_rps: float | None = None
        self._override_until = 0.0

    def set_rate(self, rps: float, duration_s: float | None) -> None:
        """Set a surge for `duration_s` seconds, or change the base rate if None."""
        rps = min(rps, self._max_rps)
        if duration_s is None:
            self._base_rps = rps
            self._override_rps = None
        else:
            self._override_rps = rps
            self._override_until = self._clock() + duration_s

    def current(self) -> float:
        if self._override_rps is not None and self._clock() >= self._override_until:
            self._override_rps = None
        return self._base_rps if self._override_rps is None else self._override_rps

    def status(self) -> dict[str, Any]:
        current = self.current()
        remaining = (
            round(self._override_until - self._clock(), 1)
            if self._override_rps is not None
            else None
        )
        return {
            "current_rps": current,
            "base_rps": self._base_rps,
            "override_remaining_s": remaining,
        }


class LoadEngine:
    """Generates the 70/30 read/write mix against the gateway."""

    def __init__(
        self,
        settings: LoadgenSettings,
        clients: list[httpx.AsyncClient],
        rate: RateController,
        record: Recorder,
        rng: random.Random | None = None,
    ) -> None:
        self._settings = settings
        self._clients = clients
        self._sent = 0
        self._rate = rate
        self._record = record
        self._rng = rng or random.Random()  # noqa: S311 - synthetic traffic, not security
        self._known_ids: deque[str] = deque(maxlen=settings.known_ids)
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    def _new_order(self) -> dict[str, Any]:
        items = [
            {
                "sku": self._rng.choice(SKUS),
                "qty": self._rng.randint(1, 4),
                "unit_price_cents": self._rng.randrange(499, 49_999, 100),
            }
            for _ in range(self._rng.randint(1, 3))
        ]
        return {"customer": self._rng.choice(CUSTOMERS), "items": items}

    async def send_one(self) -> None:
        """Send one request and record its outcome."""
        started = time.perf_counter()
        client = self._clients[self._sent % len(self._clients)]
        self._sent += 1
        try:
            if self._rng.random() >= self._settings.read_fraction:
                response = await client.post("/api/orders", json=self._new_order())
                if response.status_code == 201:
                    self._known_ids.append(response.json()["id"])
            elif self._known_ids and self._rng.random() < 0.5:
                response = await client.get(f"/api/orders/{self._rng.choice(self._known_ids)}")
            else:
                response = await client.get("/api/orders", params={"limit": 10})
            outcome = classify(response.status_code)
        except httpx.TimeoutException:
            outcome = "timeout"
        except httpx.HTTPError:
            outcome = "connect_error"
        self._record(outcome, time.perf_counter() - started)

    def _spawn(self) -> None:
        if len(self._tasks) >= self._settings.max_in_flight:
            self._record("dropped", 0.0)
            return
        task = asyncio.create_task(self.send_one())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(self) -> None:
        """Run until cancelled, pacing arrivals to the current rate."""
        next_at = time.monotonic()
        try:
            while True:
                rps = self._rate.current()
                if rps <= 0:
                    await asyncio.sleep(MAX_SLEEP_S)
                    next_at = time.monotonic()
                    continue
                next_at += self._rng.expovariate(rps)
                now = time.monotonic()
                if now - next_at > CATCH_UP_LIMIT_S:
                    next_at = now  # fell badly behind: do not burst to catch up
                delay = next_at - now
                if delay > MAX_SLEEP_S:
                    # Long gap (low rate): wake early so a rate change takes effect
                    # promptly. Arrivals are memoryless, so re-drawing the wait
                    # from "now" leaves the arrival process unchanged.
                    await asyncio.sleep(MAX_SLEEP_S)
                    next_at = time.monotonic()
                    continue
                # Always yield, even when behind schedule: without this the loop
                # would spawn requests forever and never let them run.
                await asyncio.sleep(max(delay, 0.0))
                self._spawn()
        finally:
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
