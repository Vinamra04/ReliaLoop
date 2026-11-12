"""Interfaces between the agent core and the outside world.

The pipeline, diagnosis, policy and verification code depend only on these
protocols. Tests plug in fakes; a different runtime (for example Kubernetes)
plugs in a different `ActionExecutor` without touching the core.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from agent.app.domain import ActionResult, ContainerStatus, DeployEvent


@dataclass(frozen=True)
class Sample:
    """One series of an instant query."""

    labels: Mapping[str, str]
    value: float


class MetricsSource(Protocol):
    """Read-only metrics queries."""

    async def instant(self, query: str) -> list[Sample]:
        """Run an instant query; raise `SourceError` on failure."""
        ...


class LogSource(Protocol):
    """Recent raw log lines of a service (untrusted text)."""

    async def recent(self, service: str, lines: int, since_s: int) -> list[str]: ...


class DeployHistory(Protocol):
    """Recorded deploy events, newest first."""

    def deploys(self, service: str | None = None, limit: int = 20) -> list[DeployEvent]: ...


class ActionExecutor(Protocol):
    """Changes the running system. Every method is time-bounded and returns a result."""

    async def status(self, service: str) -> list[ContainerStatus]: ...

    async def restart(self, service: str) -> ActionResult: ...

    async def scale(self, service: str, replicas: int) -> ActionResult: ...

    async def rollback(self, service: str, to_version: str) -> ActionResult: ...

    async def version_available(self, service: str, version: str) -> bool: ...


class Notifier(Protocol):
    """Publishes incident events to interested parties (log, event stream)."""

    async def publish(self, kind: str, payload: Mapping[str, Any]) -> None: ...


class SourceError(Exception):
    """An evidence source or executor backend could not be reached or failed."""
