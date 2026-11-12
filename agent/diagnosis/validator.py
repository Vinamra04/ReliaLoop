"""Deterministic validation of a diagnosis before policy sees it.

Runs on rule-based and LLM output alike and trusts neither. Anything that
fails here is rejected and the incident escalates. The checks:

- the services named exist and the target is inside the incident's scope;
- every cited evidence id exists in the bundle (anti-hallucination);
- the action is allowed for the target by the CMDB and the target is managed;
- confidence is high enough and the diagnosis does not flag itself as unsure;
- the action is consistent with the category and with the facts (a rollback
  needs a recent deploy and a previous version; a scale-out needs headroom).
"""

from __future__ import annotations

from agent.app.config import AgentSettings
from agent.app.domain import (
    EXECUTABLE_ACTIONS,
    ActionType,
    Category,
    Diagnosis,
    EvidenceBundle,
    ProposedAction,
    ValidationResult,
)
from libs.core.cmdb import Cmdb

# Which executable actions make sense for which category.
CONSISTENT_ACTIONS: dict[Category, frozenset[ActionType]] = {
    Category.ERROR_SPIKE: frozenset({ActionType.RESTART}),
    Category.LOAD_SATURATION: frozenset({ActionType.SCALE}),
    Category.CRASH_LOOP: frozenset({ActionType.ROLLBACK, ActionType.RESTART}),
    Category.MEMORY_LEAK: frozenset({ActionType.RESTART}),
    Category.BAD_DEPLOY: frozenset({ActionType.ROLLBACK}),
    Category.UNKNOWN: frozenset(),
}


def _fail(reason: str, detail: str) -> ValidationResult:
    return ValidationResult(ok=False, reason=reason, detail=detail)


def _check_rollback(
    action: ProposedAction, bundle: EvidenceBundle, settings: AgentSettings
) -> ValidationResult | ProposedAction:
    target = action.target_service
    history = [d for d in bundle.deploys if d.service == target]
    releases = [d for d in history if d.reason != "rollback"]
    if not releases:
        return _fail("rollback_without_recent_deploy", f"no deploy of {target} is recorded")
    if history[0].reason == "rollback":
        return _fail(
            "rollback_without_recent_deploy", f"the last deploy of {target} was already rolled back"
        )
    latest = releases[0]
    age = (bundle.collected_at - latest.ts).total_seconds()
    if age > settings.rollback_lookback_s:
        return _fail(
            "rollback_without_recent_deploy",
            f"last deploy of {target} was {age:.0f}s ago (limit {settings.rollback_lookback_s}s)",
        )
    if not latest.previous_version or latest.previous_version == latest.version:
        return _fail("rollback_no_previous_version", f"{target} has no previous version")
    requested = action.params.get("to_version")
    if requested not in (None, latest.previous_version):
        return _fail(
            "rollback_wrong_version",
            f"can only roll back to {latest.previous_version}, not {requested}",
        )
    return action.model_copy(update={"params": {"to_version": latest.previous_version}})


def _check_scale(
    action: ProposedAction, bundle: EvidenceBundle, cmdb: Cmdb
) -> ValidationResult | ProposedAction:
    target = action.target_service
    limits = cmdb.services[target].limits
    current = int(
        bundle.value("containers_running", target) or bundle.value("replicas_up", target) or 0
    )
    if current < 1:
        return _fail("scale_unknown_replicas", f"current replica count of {target} is unknown")
    if current >= limits.max_replicas:
        return _fail(
            "scale_at_max_replicas", f"{target} already runs {current} of {limits.max_replicas}"
        )
    requested = action.params.get("replicas", current + 1)
    if not isinstance(requested, int) or isinstance(requested, bool) or requested != current + 1:
        return _fail("scale_invalid_replicas", f"can only scale {target} by one: {current + 1}")
    return action.model_copy(update={"params": {"replicas": requested}})


def validate(
    diagnosis: Diagnosis, bundle: EvidenceBundle, cmdb: Cmdb, settings: AgentSettings
) -> ValidationResult:
    """Return whether the diagnosis may proceed to policy, with the normalised action."""
    action = diagnosis.action
    target = action.target_service

    if diagnosis.root_cause_service not in cmdb.services:
        return _fail(
            "unknown_service", f"unknown root cause service {diagnosis.root_cause_service}"
        )

    unknown = sorted({c.id for c in diagnosis.evidence} - bundle.ids())
    if unknown:
        return _fail("unknown_evidence_id", f"cited evidence not in the bundle: {unknown[:5]}")

    if action.type not in EXECUTABLE_ACTIONS:
        # escalate / no_action change nothing; the pipeline hands over to a human.
        return ValidationResult(ok=True, action=action)

    if target not in cmdb.services:
        return _fail("unknown_service", f"unknown target service {target}")
    service = cmdb.services[target]
    if not service.managed:
        return _fail("unmanaged_target", f"{target} is not managed by the agent")
    if action.type.value not in service.allowed_actions:
        return _fail("action_not_allowed", f"{action.type.value} is not allowed on {target}")
    if target not in bundle.services:
        return _fail("target_out_of_scope", f"{target} is not part of this incident's services")
    if not diagnosis.evidence:
        return _fail("no_evidence_cited", "an action needs at least one cited evidence item")
    if diagnosis.insufficient_evidence:
        return _fail(
            "insufficient_evidence", "the diagnosis flags its own evidence as insufficient"
        )
    if diagnosis.confidence < settings.min_confidence:
        return _fail(
            "low_confidence",
            f"confidence {diagnosis.confidence:.2f} is below {settings.min_confidence:.2f}",
        )
    if action.type not in CONSISTENT_ACTIONS[diagnosis.category]:
        return _fail(
            "action_inconsistent_with_category",
            f"{action.type.value} does not fit {diagnosis.category.value}",
        )

    checked: ValidationResult | ProposedAction = action
    if action.type is ActionType.ROLLBACK:
        checked = _check_rollback(action, bundle, settings)
    elif action.type is ActionType.SCALE:
        checked = _check_scale(action, bundle, cmdb)
    else:
        checked = action.model_copy(update={"params": {}})
    if isinstance(checked, ValidationResult):
        return checked
    return ValidationResult(ok=True, action=checked)
