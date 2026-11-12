"""Prometheus adapter for the `MetricsSource` port."""

from __future__ import annotations

import math

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from agent.ports.protocols import Sample, SourceError

RETRY_ATTEMPTS = 2
RETRY_MAX_WAIT_S = 1.0


class PrometheusMetrics:
    """Instant queries over the HTTP API. Reads are idempotent, so they are retried once."""

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._url = f"{base_url.rstrip('/')}/api/v1/query"

    async def instant(self, query: str) -> list[Sample]:
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(RETRY_ATTEMPTS),
                wait=wait_random_exponential(multiplier=0.1, max=RETRY_MAX_WAIT_S),
                retry=retry_if_exception_type(httpx.TransportError),
                reraise=True,
            ):
                with attempt:
                    response = await self._client.get(self._url, params={"query": query})
        except httpx.HTTPError as exc:
            raise SourceError(f"prometheus unreachable: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise SourceError(f"prometheus returned {response.status_code}")
        body = response.json()
        if body.get("status") != "success":
            raise SourceError(f"prometheus query failed: {body.get('error', 'unknown error')}")
        samples = []
        for series in body["data"]["result"]:
            value = float(series["value"][1])
            # NaN and infinities carry no usable evidence (for example 0/0).
            if math.isfinite(value):
                samples.append(Sample(labels=dict(series["metric"]), value=value))
        return samples

    async def ready(self) -> bool:
        """True when Prometheus answers a trivial query."""
        try:
            await self.instant("vector(1)")
        except SourceError:
            return False
        return True
