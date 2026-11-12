"""Replica discovery through DNS.

All replicas of a service share one network alias, so its A records are the
replica list. Resolving per request (with a short cache) and picking at
random spreads load across replicas, including ones added a moment ago.
"""

from __future__ import annotations

import asyncio
import random
import socket
import time
from collections.abc import Awaitable, Callable

Lookup = Callable[[str, int], Awaitable[list[str]]]


class NoReplicasError(Exception):
    """The service name resolved to no addresses."""


async def dns_lookup(host: str, port: int) -> list[str]:
    """Resolve every IPv4 address for `host`."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


class ReplicaResolver:
    """Caches the replica list briefly and hands out a random replica."""

    def __init__(
        self,
        host: str,
        port: int,
        ttl_s: float,
        lookup: Lookup = dns_lookup,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._ttl_s = ttl_s
        self._lookup = lookup
        self._clock = clock
        self._rng = rng or random.Random()  # noqa: S311 - load spreading, not security
        self._cached: list[str] = []
        self._expires_at = 0.0

    async def replicas(self) -> list[str]:
        """Return base URLs of all replicas, refreshing the cache when stale."""
        now = self._clock()
        if not self._cached or now >= self._expires_at:
            try:
                addresses = await self._lookup(self._host, self._port)
            except OSError as exc:
                self._cached = []
                raise NoReplicasError(str(exc)) from exc
            if not addresses:
                raise NoReplicasError(f"{self._host} has no addresses")
            self._cached = [f"http://{address}:{self._port}" for address in addresses]
            self._expires_at = now + self._ttl_s
        return self._cached

    async def pick(self) -> str:
        """Return the base URL of one replica chosen at random."""
        return self._rng.choice(await self.replicas())
