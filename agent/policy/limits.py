"""Policy configuration and the persisted counters behind the limits."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field

Risk = Literal["low", "high"]

SCOPE_ACTION = "action"  # any executed action, keyed by target service
SCOPE_RESTART = "restart"  # executed restarts, keyed by target service
SCOPE_INCIDENT = "incident"  # executed actions, keyed by incident id
SCOPE_GLOBAL = "global"
GLOBAL_KEY = "all"
HOUR_S = 3600


class PolicyError(ValueError):
    """The policy file is invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WindowLimit(_Strict):
    max: int = Field(ge=0)
    window_s: int = Field(gt=0)


class Approval(_Strict):
    required_for: tuple[Risk, ...] = ("high",)
    timeout_s: int = Field(default=600, gt=0)


class Limits(_Strict):
    restarts_per_service: WindowLimit = WindowLimit(max=2, window_s=900)
    actions_per_incident: int = Field(default=2, ge=0)
    actions_per_hour: int = Field(default=30, ge=0)
    cooldown_s: int = Field(default=60, ge=0)


class Policy(_Strict):
    risk: dict[str, Risk]
    approval: Approval = Approval()
    limits: Limits = Limits()

    def risk_of(self, action: str) -> Risk:
        """Risk class of an action; anything unlisted is high risk."""
        return self.risk.get(action, "high")


def load_policy(path: Path) -> Policy:
    try:
        return Policy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise PolicyError(f"invalid policy {path}: {exc}") from exc


class LimitStore(Protocol):
    """The slice of the store the limits need. Persisted, so limits survive restarts."""

    def add_limit_event(self, scope: str, key: str, ts: datetime) -> None: ...
    def count_limit_events(self, scope: str, key: str, since: datetime) -> int: ...
    def last_limit_event(self, scope: str, key: str) -> datetime | None: ...


def record_execution(
    store: LimitStore, *, action: str, target: str, incident_id: str, now: datetime
) -> None:
    """Count one executed action against every limit it affects."""
    store.add_limit_event(SCOPE_ACTION, target, now)
    store.add_limit_event(SCOPE_INCIDENT, incident_id, now)
    store.add_limit_event(SCOPE_GLOBAL, GLOBAL_KEY, now)
    if action == "restart_service":
        store.add_limit_event(SCOPE_RESTART, target, now)


def window_start(now: datetime, seconds: int) -> datetime:
    return now - timedelta(seconds=seconds)
