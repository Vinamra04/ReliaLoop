"""Domain types shared by every stage of the incident pipeline."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class State(StrEnum):
    """Incident state machine (see `state_machine.py` for the allowed transitions)."""

    RECEIVED = "RECEIVED"
    ENRICHED = "ENRICHED"
    DIAGNOSED = "DIAGNOSED"
    VALIDATED = "VALIDATED"
    POLICY_DECIDED = "POLICY_DECIDED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    ACTING = "ACTING"
    VERIFYING = "VERIFYING"
    FAILED_VERIFICATION = "FAILED_VERIFICATION"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    REJECTED = "REJECTED"
    DRY_RUN_COMPLETE = "DRY_RUN_COMPLETE"
    OBSERVED = "OBSERVED"


class Category(StrEnum):
    ERROR_SPIKE = "ERROR_SPIKE"
    LOAD_SATURATION = "LOAD_SATURATION"
    CRASH_LOOP = "CRASH_LOOP"
    MEMORY_LEAK = "MEMORY_LEAK"
    BAD_DEPLOY = "BAD_DEPLOY"
    UNKNOWN = "UNKNOWN"


class ActionType(StrEnum):
    RESTART = "restart_service"
    SCALE = "scale_service"
    ROLLBACK = "rollback_service"
    NO_ACTION = "no_action"
    ESCALATE = "escalate"


# Actions that change the system (the others only end the pipeline).
EXECUTABLE_ACTIONS = frozenset({ActionType.RESTART, ActionType.SCALE, ActionType.ROLLBACK})


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class EvidenceItem(_Frozen):
    """One observation. Diagnoses may cite it by `id` and nothing else."""

    id: str
    service: str
    name: str
    window: str = ""
    value: float | str | None
    unit: str = ""
    observed_at: datetime
    source: str = "prometheus"


class LogSummary(_Frozen):
    """Deduplicated, redacted and size-capped view of a service's recent logs."""

    service: str
    lines_seen: int
    error_templates: tuple[tuple[str, int], ...] = ()
    tail: tuple[str, ...] = ()
    truncated: bool = False


class DeployEvent(_Frozen):
    service: str
    version: str
    previous_version: str | None = None
    status: str
    reason: str = "deploy"
    incident_id: str | None = None
    override_reason: str | None = None
    actor: str = "cli"
    ts: datetime


class EvidenceBundle(_Frozen):
    """Everything the diagnosis may use. Built deterministically, never by an LLM."""

    incident_id: str
    alerting_service: str
    slo: str | None
    services: tuple[str, ...]
    collected_at: datetime
    items: tuple[EvidenceItem, ...]
    logs: tuple[LogSummary, ...] = ()
    deploys: tuple[DeployEvent, ...] = ()
    # Sources that failed or timed out; the bundle is then partial.
    incomplete: tuple[str, ...] = ()

    def ids(self) -> frozenset[str]:
        return frozenset(item.id for item in self.items)

    def value(self, name: str, service: str) -> float | None:
        """Numeric value of `name@service`, or None when absent or not numeric."""
        for item in self.items:
            if item.name == name and item.service == service:
                return float(item.value) if isinstance(item.value, int | float) else None
        return None

    def text(self, name: str, service: str) -> str | None:
        for item in self.items:
            if item.name == name and item.service == service and isinstance(item.value, str):
                return item.value
        return None


class Citation(_Frozen):
    id: str
    observation: str = Field(max_length=300)


class ProposedAction(_Frozen):
    type: ActionType
    target_service: str
    params: dict[str, Any] = Field(default_factory=dict)


class Diagnosis(_Frozen):
    """Output contract of both the rule-based engine and the LLM."""

    category: Category
    root_cause_service: str
    confidence: float = Field(ge=0, le=1)
    insufficient_evidence: bool = False
    evidence: tuple[Citation, ...] = ()
    need_more_evidence: tuple[str, ...] = ()
    action: ProposedAction
    rationale: str = Field(max_length=600)


class DiagnosisRecord(_Frozen):
    """A diagnosis plus how it was produced."""

    diagnosis: Diagnosis
    mode: Literal["rules", "llm"]
    provider: str = "rules"
    model: str = ""
    prompt_version: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    degraded_mode: bool = False


class ValidationResult(_Frozen):
    ok: bool
    reason: str = ""
    detail: str = ""
    # The action after validation; low confidence downgrades it to escalate.
    action: ProposedAction | None = None


class Decision(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "approval_required"
    DENY = "deny"
    DRY_RUN = "dry_run"
    COVERED = "covered"


class PolicyDecision(_Frozen):
    decision: Decision
    rule: str
    detail: str = ""
    risk: str = "low"
    # Set when another incident's action already covers this root cause.
    covered_by: str | None = None


class ContainerStatus(_Frozen):
    name: str
    image: str
    state: str
    health: str
    restart_count: int
    started_at: str = ""
    oom_killed: bool = False


class ActionResult(_Frozen):
    success: bool
    detail: str
    duration_s: float = 0.0
    data: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(_Frozen):
    recovered: bool
    checks: int
    detail: str
    duration_s: float = 0.0


class Incident(BaseModel):
    """Incident row. Mutable: the pipeline updates and persists it."""

    id: str
    service: str
    slo: str | None = None
    severity: str = "page"
    state: State = State.RECEIVED
    status: Literal["open", "closed"] = "open"
    mode: str = "dry_run"
    diagnosis_mode: str = "rules"
    category: Category | None = None
    root_cause_service: str | None = None
    degraded_mode: bool = False
    replans: int = 0
    summary: str = ""
    resolution: str | None = None
    escalation_reason: str | None = None
    opened_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None


TERMINAL_STATES = frozenset(
    {State.RESOLVED, State.ESCALATED, State.REJECTED, State.DRY_RUN_COMPLETE, State.OBSERVED}
)
