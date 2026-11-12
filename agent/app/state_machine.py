"""Incident state machine.

`RECEIVED -> ENRICHED -> DIAGNOSED -> VALIDATED -> POLICY_DECIDED ->
[PENDING_APPROVAL] -> ACTING -> VERIFYING -> RESOLVED`, with side exits.
Every transition is validated here and persisted as a timeline event, so an
incident's history can always be replayed.
"""

from __future__ import annotations

from agent.app.domain import State

_S = State

TRANSITIONS: dict[State, frozenset[State]] = {
    _S.RECEIVED: frozenset({_S.ENRICHED, _S.ESCALATED}),
    _S.ENRICHED: frozenset({_S.DIAGNOSED, _S.ESCALATED}),
    _S.DIAGNOSED: frozenset({_S.VALIDATED, _S.OBSERVED, _S.ESCALATED}),
    _S.VALIDATED: frozenset({_S.POLICY_DECIDED, _S.ESCALATED}),
    _S.POLICY_DECIDED: frozenset(
        {_S.ACTING, _S.PENDING_APPROVAL, _S.DRY_RUN_COMPLETE, _S.VERIFYING, _S.ESCALATED}
    ),
    # VERIFYING directly: another incident executed the same action meanwhile.
    _S.PENDING_APPROVAL: frozenset({_S.ACTING, _S.VERIFYING, _S.REJECTED, _S.ESCALATED}),
    _S.ACTING: frozenset({_S.VERIFYING, _S.ESCALATED}),
    _S.VERIFYING: frozenset({_S.RESOLVED, _S.FAILED_VERIFICATION, _S.ESCALATED}),
    # One re-plan is allowed: back to evidence collection, otherwise escalate.
    _S.FAILED_VERIFICATION: frozenset({_S.ENRICHED, _S.ESCALATED}),
    _S.RESOLVED: frozenset(),
    _S.ESCALATED: frozenset(),
    _S.REJECTED: frozenset(),
    _S.DRY_RUN_COMPLETE: frozenset(),
    _S.OBSERVED: frozenset(),
}


class InvalidTransitionError(Exception):
    """A transition the state machine does not allow."""


def check_transition(current: State, new: State) -> None:
    """Raise `InvalidTransitionError` unless `current -> new` is allowed."""
    if new not in TRANSITIONS[current]:
        raise InvalidTransitionError(f"{current.value} -> {new.value} is not allowed")
