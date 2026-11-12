"""The output contract given to the LLM, and its conversion to the domain model.

The schema uses only plain types: provider-side schema support is narrower
than pydantic's, and every real check (ranges, known services, cited ids)
happens afterwards in the validator anyway.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.app.domain import ActionType, Category, Citation, Diagnosis, ProposedAction

MAX_RATIONALE = 600
MAX_OBSERVATION = 300
MAX_CITATIONS = 12
MAX_EXTRA_REQUESTS = 6

CategoryName = Literal[
    "ERROR_SPIKE", "LOAD_SATURATION", "CRASH_LOOP", "MEMORY_LEAK", "BAD_DEPLOY", "UNKNOWN"
]
ActionName = Literal[
    "restart_service", "scale_service", "rollback_service", "no_action", "escalate"
]


class LlmCitation(BaseModel):
    id: str = Field(description="An evidence id exactly as it appears in the evidence table.")
    observation: str = Field(description="What this evidence shows, in a few words.")


class LlmAction(BaseModel):
    type: ActionName
    target_service: str
    replicas: int | None = Field(
        default=None, description="scale_service only: the new replica count (current + 1)."
    )
    to_version: str | None = Field(
        default=None, description="rollback_service only: the previous version to return to."
    )


class LlmDiagnosis(BaseModel):
    """What the model must return."""

    category: CategoryName
    root_cause_service: str
    confidence: float = Field(description="0.0 to 1.0.")
    insufficient_evidence: bool
    evidence: list[LlmCitation]
    need_more_evidence: list[str] = Field(
        description="Names from the optional evidence catalog; empty when nothing more is needed."
    )
    action: LlmAction
    rationale: str = Field(description="At most 600 characters.")


class LlmPostmortem(BaseModel):
    """AI-drafted narrative sections of a postmortem."""

    summary: str
    what_went_well: list[str]
    what_went_badly: list[str]
    action_items: list[str]


def to_domain(raw: LlmDiagnosis) -> Diagnosis:
    """Convert model output to the domain type, trimming over-long text.

    Values are clamped or truncated rather than rejected here; whether the
    diagnosis is *acceptable* is the validator's decision, not this function's.
    """
    params: dict[str, Any] = {}
    if raw.action.type == "scale_service" and raw.action.replicas is not None:
        params["replicas"] = raw.action.replicas
    if raw.action.type == "rollback_service" and raw.action.to_version:
        params["to_version"] = raw.action.to_version
    return Diagnosis(
        category=Category(raw.category),
        root_cause_service=raw.root_cause_service.strip()[:63],
        confidence=min(1.0, max(0.0, raw.confidence)),
        insufficient_evidence=raw.insufficient_evidence,
        evidence=tuple(
            Citation(id=item.id.strip()[:200], observation=item.observation[:MAX_OBSERVATION])
            for item in raw.evidence[:MAX_CITATIONS]
        ),
        need_more_evidence=tuple(raw.need_more_evidence[:MAX_EXTRA_REQUESTS]),
        action=ProposedAction(
            type=ActionType(raw.action.type),
            target_service=raw.action.target_service.strip()[:63],
            params=params,
        ),
        rationale=raw.rationale[:MAX_RATIONALE],
    )
