"""CMDB model: the validated shape of `cmdb/services.yaml`.

Shared by the rule generator (memory limits), the agent (policy and
root-cause localisation) and the console (service cards).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

ActionType = Literal["restart_service", "scale_service", "rollback_service"]


class CmdbError(ValueError):
    """The CMDB file is invalid."""


class ReplicaLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_replicas: int = Field(default=1, ge=1)
    max_replicas: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> ReplicaLimits:
        if self.max_replicas < self.min_replicas:
            raise ValueError("max_replicas must be >= min_replicas")
        return self


class Service(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str = ""
    tier: int = Field(ge=0, le=3)
    owner: str = Field(min_length=1)
    contacts: tuple[str, ...] = ()
    managed: bool
    compose_service: str | None = None
    depends_on: tuple[str, ...] = ()
    slos: tuple[str, ...] = ()
    runbooks: tuple[str, ...] = ()
    dashboards: tuple[str, ...] = ()
    allowed_actions: tuple[ActionType, ...] = ()
    limits: ReplicaLimits = ReplicaLimits()
    memory_limit_mb: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _unmanaged_has_no_actions(self) -> Service:
        if not self.managed and self.allowed_actions:
            raise ValueError("an unmanaged service must not allow any action")
        if self.managed and not self.compose_service:
            raise ValueError("a managed service needs compose_service")
        return self


class Cmdb(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    services: dict[str, Service]

    @model_validator(mode="after")
    def _dependencies_exist(self) -> Cmdb:
        for name, service in self.services.items():
            unknown = [dep for dep in service.depends_on if dep not in self.services]
            if unknown:
                raise ValueError(f"{name} depends on unknown services: {', '.join(unknown)}")
        for name in self.services:
            self._check_acyclic(name, ())
        return self

    def _check_acyclic(self, name: str, path: tuple[str, ...]) -> None:
        if name in path:
            raise ValueError(f"dependency cycle: {' -> '.join((*path, name))}")
        for dep in self.services[name].depends_on:
            self._check_acyclic(dep, (*path, name))

    def dependency_chain(self, name: str) -> list[str]:
        """`name` followed by everything it depends on, nearest first, no duplicates."""
        ordered: list[str] = []
        queue = [name]
        while queue:
            current = queue.pop(0)
            if current in ordered or current not in self.services:
                continue
            ordered.append(current)
            queue.extend(self.services[current].depends_on)
        return ordered

    def depth(self, name: str) -> int:
        """Longest dependency path below `name` (0 for a leaf). Higher = more upstream."""
        deps = self.services[name].depends_on
        return 0 if not deps else 1 + max(self.depth(dep) for dep in deps)


def load_cmdb(path: Path) -> Cmdb:
    """Load and validate the CMDB; raise `CmdbError` with the reason on failure."""
    try:
        return Cmdb.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise CmdbError(f"invalid CMDB {path}: {exc}") from exc
