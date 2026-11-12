"""Kubernetes executor: a deliberate stub.

It exists to show where a second runtime plugs in. The agent core depends on
the `ActionExecutor` port only, so supporting Kubernetes means implementing
this class and nothing else. See `docs/adr/0002-executor-abstraction.md`.
"""

from __future__ import annotations

from agent.app.domain import ActionResult, ContainerStatus

_NOT_BUILT = "KubernetesExecutor is a stub: ReliaLoop runs on Docker Compose (see ADR 0002)"


class KubernetesExecutor:
    """Not implemented. Each method notes the Kubernetes operation it would map to."""

    async def status(self, service: str) -> list[ContainerStatus]:
        """Would list the Deployment's pods with phase, readiness and restart counts."""
        raise NotImplementedError(_NOT_BUILT)

    async def restart(self, service: str) -> ActionResult:
        """Would patch the pod template annotation (`kubectl rollout restart`) and watch."""
        raise NotImplementedError(_NOT_BUILT)

    async def scale(self, service: str, replicas: int) -> ActionResult:
        """Would patch the Deployment's scale subresource and wait for ready replicas."""
        raise NotImplementedError(_NOT_BUILT)

    async def rollback(self, service: str, to_version: str) -> ActionResult:
        """Would set the previous image on the Deployment (or `rollout undo`) and watch."""
        raise NotImplementedError(_NOT_BUILT)

    async def version_available(self, service: str, version: str) -> bool:
        """Would check the registry or the Deployment's revision history."""
        raise NotImplementedError(_NOT_BUILT)
