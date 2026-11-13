"""Scenario and run-result models."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

Mode = Literal["none", "rules", "llm"]
MODES: tuple[Mode, ...] = ("none", "rules", "llm")
SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GroundTruth(_Strict):
    category: str
    root_cause_service: str
    expected_action: str


class Injection(_Strict):
    kind: Literal["chaos_errors", "traffic_surge", "deploy"]
    service: str = "orders"
    rate: float = 0.5
    rps: float = 150
    version: str = ""
    duration_s: int = 900


class Scenario(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str
    ground_truth: GroundTruth
    inject: Injection
    max_duration_s: int = Field(default=300, gt=0)
    control_duration_s: int = Field(default=150, gt=0)


def load_scenarios(directory: Path = SCENARIOS_DIR) -> dict[str, Scenario]:
    """Every scenario in the directory, keyed by name."""
    scenarios = [
        Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        for path in sorted(directory.glob("*.yaml"))
    ]
    return {scenario.name: scenario for scenario in scenarios}


class RunResult(BaseModel):
    """Everything measured for one run. `None` always means "not measured"."""

    scenario: str
    mode: Mode
    run: int = 1
    started_at: str
    t_inject: float
    duration_s: float
    error: str | None = None

    # detection and recovery (same method for every mode)
    detected: bool = False
    time_to_detect_s: float | None = None
    time_to_mitigate_s: float | None = None
    mitigate_censored: bool = False
    error_budget_consumed_pct: float | None = None

    # diagnosis (agent modes only)
    incident_ids: list[str] = Field(default_factory=list)
    primary_incident: str | None = None
    final_state: str | None = None
    diagnosed_category: str | None = None
    diagnosed_root_cause: str | None = None
    proposed_action: str | None = None
    diagnosis_correct: bool | None = None
    root_cause_correct: bool | None = None
    action_correct: bool | None = None
    degraded_mode: bool = False

    # safety and outcome
    actions_executed: list[str] = Field(default_factory=list)
    wrong_actions_executed: int = 0
    unsafe_actions_blocked: int = 0
    approvals_given: int = 0
    recovery_verified: bool | None = None

    # LLM cost (llm mode only)
    llm_calls: int = 0
    llm_tokens_in: int = 0
    llm_tokens_out: int = 0
    llm_latency_ms: float | None = None
    llm_model: str | None = None
