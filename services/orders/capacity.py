"""Capacity model: a fixed number of worker slots, so saturation is real.

With W slots and T seconds of work per request a replica serves about W/T
requests per second. Beyond that, requests queue for a slot, latency rises,
and requests that wait too long are shed. Adding a replica adds W slots,
which is why scaling out genuinely fixes a load surge.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager


class Overloaded(Exception):  # noqa: N818 - reads naturally at the raise site
    """No worker slot became free within the queue timeout."""


class UtilizationTracker:
    """Time-weighted share of busy worker slots over a sliding window.

    An instantaneous busy/total reading taken at scrape time would be very
    noisy; integrating busy-time gives a stable saturation signal.
    """

    def __init__(
        self, workers: int, window_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._workers = workers
        self._window_s = window_s
        self._clock = clock
        self._busy = 0
        self._busy_seconds = 0.0
        self._last_change = clock()
        self._samples: deque[tuple[float, float]] = deque([(self._last_change, 0.0)])

    def _integrate(self) -> float:
        now = self._clock()
        self._busy_seconds += self._busy * (now - self._last_change)
        self._last_change = now
        return now

    def acquire(self) -> None:
        self._integrate()
        self._busy += 1

    def release(self) -> None:
        self._integrate()
        self._busy -= 1

    def sample(self) -> float:
        """Record a sample and return utilization (0-1) over the window."""
        now = self._integrate()
        self._samples.append((now, self._busy_seconds))
        while len(self._samples) > 2 and now - self._samples[1][0] >= self._window_s:
            self._samples.popleft()
        start_time, start_busy = self._samples[0]
        elapsed = now - start_time
        if elapsed <= 0:
            return 0.0
        return min(1.0, (self._busy_seconds - start_busy) / (elapsed * self._workers))


class WorkerPool:
    """Bounded concurrency with a bounded wait."""

    def __init__(
        self,
        workers: int,
        queue_timeout_s: float,
        tracker: UtilizationTracker,
        on_wait: Callable[[float], None],
    ) -> None:
        self._semaphore = asyncio.Semaphore(workers)
        self._queue_timeout_s = queue_timeout_s
        self._tracker = tracker
        self._on_wait = on_wait

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold one worker slot; raise `Overloaded` if none frees up in time."""
        started = time.perf_counter()
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._queue_timeout_s)
        except TimeoutError as exc:
            self._on_wait(time.perf_counter() - started)
            raise Overloaded from exc
        self._on_wait(time.perf_counter() - started)
        self._tracker.acquire()
        try:
            yield
        finally:
            self._tracker.release()
            self._semaphore.release()
