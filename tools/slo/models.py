"""SLO spec models and loading."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

SELECTOR_PATTERN = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(\{(?P<matchers>.*)\})?$")
DURATION_PATTERN = re.compile(r"^\d+[smhdw]$")


class SpecError(ValueError):
    """An SLO spec is invalid."""


def _check_selector(value: str) -> str:
    if not SELECTOR_PATTERN.match(value.strip()):
        raise ValueError(f"not a metric selector: {value!r}")
    return value.strip()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AvailabilitySli(_Strict):
    """Good events = total minus errors."""

    type: Literal["availability"]
    total: str = Field(description="Selector counting all valid events.")
    errors: str = Field(description="Selector counting bad events.")

    _selectors = field_validator("total", "errors")(_check_selector)


class LatencySli(_Strict):
    """Good events = requests faster than the threshold."""

    type: Literal["latency"]
    histogram: str = Field(description="Histogram base name with optional matchers.")
    threshold_seconds: float = Field(gt=0, description="Must be an existing bucket boundary.")

    _selectors = field_validator("histogram")(_check_selector)


class Alerting(_Strict):
    profile: Literal["demo", "production"]
    min_traffic_rps: float = Field(ge=0, description="No alert below this request rate.")
    runbook: str = Field(pattern=r"^[a-z0-9-]+$")
    owner: str = Field(min_length=1)


class SloSpec(_Strict):
    """One service level objective."""

    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    service: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    description: str = Field(min_length=1)
    sli: Annotated[AvailabilitySli | LatencySli, Field(discriminator="type")]
    objective: float = Field(gt=50, lt=100, description="Percent of good events.")
    window: str = Field(pattern=DURATION_PATTERN.pattern, description="Production SLO window.")
    alerting: Alerting

    @property
    def error_budget(self) -> float:
        """Allowed share of bad events, e.g. 0.005 for a 99.5% objective."""
        return round(1 - self.objective / 100, 10)


def load_spec(path: Path) -> SloSpec:
    """Load and validate one spec file."""
    try:
        return SloSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (yaml.YAMLError, ValueError) as exc:
        raise SpecError(f"{path.name}: {exc}") from exc


def load_specs(directory: Path) -> list[SloSpec]:
    """Load every `*.yaml` spec, sorted by name, rejecting duplicates."""
    specs = [load_spec(path) for path in sorted(directory.glob("*.yaml"))]
    if not specs:
        raise SpecError(f"no SLO specs found in {directory}")
    names = [spec.name for spec in specs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise SpecError(f"duplicate SLO names: {', '.join(duplicates)}")
    return sorted(specs, key=lambda spec: spec.name)
