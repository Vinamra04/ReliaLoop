"""Policy engine: decides whether a validated action may run.

Order of evaluation (first match wins):

1. target must be a managed service;
2. another incident is already running the same action on the target ->
   `covered` (wait for that action instead of repeating it);
3. any other in-flight action on the target -> deny;
4. cooldown after the last action on the target (same action by another
   incident inside the cooldown is also `covered`);
5. per-incident, per-hour and per-service restart limits;
6. replica ceiling from the CMDB;
7. dry-run mode records the intent and stops;
8. high-risk actions need human approval; low-risk actions run.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from agent.app.domain import ActionType, Decision, PolicyDecision, ProposedAction
from agent.policy.limits import (
    GLOBAL_KEY,
    HOUR_S,
    SCOPE_ACTION,
    SCOPE_GLOBAL,
    SCOPE_INCIDENT,
    SCOPE_RESTART,
    LimitStore,
    Policy,
    window_start,
)
from libs.core.cmdb import Cmdb


class PolicyStore(LimitStore, Protocol):
    def in_flight_action(self, target: str) -> dict[str, Any] | None: ...
    def last_executed_action(self, target: str) -> dict[str, Any] | None: ...


class PolicyEngine:
    def __init__(self, policy: Policy, cmdb: Cmdb, store: PolicyStore) -> None:
        self.policy = policy
        self._cmdb = cmdb
        self._store = store

    def evaluate(
        self, action: ProposedAction, *, incident_id: str, mode: str, now: datetime
    ) -> PolicyDecision:
        """Decide on one validated, executable action."""
        target = action.target_service
        kind = action.type.value
        risk = self.policy.risk_of(kind)
        limits = self.policy.limits

        def deny(rule: str, detail: str) -> PolicyDecision:
            return PolicyDecision(decision=Decision.DENY, rule=rule, detail=detail, risk=risk)

        def covered(by: str, rule: str) -> PolicyDecision:
            return PolicyDecision(
                decision=Decision.COVERED,
                rule=rule,
                detail=f"{kind} on {target} is already handled by {by}",
                risk=risk,
                covered_by=by,
            )

        service = self._cmdb.services.get(target)
        if service is None or not service.managed:
            return deny("unmanaged_service", f"{target} is not managed by the agent")

        running = self._store.in_flight_action(target)
        if running and running["incident_id"] != incident_id:
            if running["type"] == kind:
                return covered(running["incident_id"], "same_action_in_flight")
            return deny(
                "action_in_flight",
                f"{running['type']} on {target} is in flight for {running['incident_id']}",
            )

        last_at = self._store.last_limit_event(SCOPE_ACTION, target)
        if last_at and (now - last_at).total_seconds() < limits.cooldown_s:
            previous = self._store.last_executed_action(target)
            if previous and previous["incident_id"] != incident_id and previous["type"] == kind:
                return covered(previous["incident_id"], "same_action_in_cooldown")
            remaining = limits.cooldown_s - (now - last_at).total_seconds()
            return deny("cooldown", f"{target} is in cooldown for another {remaining:.0f}s")

        if (
            self._store.count_limit_events(
                SCOPE_INCIDENT, incident_id, window_start(now, HOUR_S * 24)
            )
            >= limits.actions_per_incident
        ):
            return deny(
                "actions_per_incident", f"limit of {limits.actions_per_incident} actions reached"
            )

        if (
            self._store.count_limit_events(SCOPE_GLOBAL, GLOBAL_KEY, window_start(now, HOUR_S))
            >= limits.actions_per_hour
        ):
            return deny("actions_per_hour", f"global limit of {limits.actions_per_hour}/h reached")

        if action.type is ActionType.RESTART:
            window = limits.restarts_per_service
            restarts = self._store.count_limit_events(
                SCOPE_RESTART, target, window_start(now, window.window_s)
            )
            if restarts >= window.max:
                return deny(
                    "restart_limit",
                    f"{target} was restarted {restarts} times in {window.window_s}s",
                )

        if action.type is ActionType.SCALE:
            replicas = action.params.get("replicas")
            ceiling = service.limits.max_replicas
            if not isinstance(replicas, int) or replicas > ceiling:
                return deny("max_replicas", f"{target} may run at most {ceiling} replicas")
            if replicas < service.limits.min_replicas:
                return deny(
                    "min_replicas", f"{target} needs {service.limits.min_replicas} replicas"
                )

        if mode == "dry_run":
            return PolicyDecision(
                decision=Decision.DRY_RUN,
                rule="dry_run_mode",
                detail=f"would execute {kind} on {target}",
                risk=risk,
            )
        if mode != "enforce":
            return deny("mode_does_not_execute", f"mode {mode} never executes actions")

        if risk in self.policy.approval.required_for:
            return PolicyDecision(
                decision=Decision.REQUIRE_APPROVAL,
                rule=f"{risk}_risk_needs_approval",
                detail=f"{kind} is {risk} risk and needs human approval",
                risk=risk,
            )
        return PolicyDecision(
            decision=Decision.ALLOW,
            rule=f"{risk}_risk_auto",
            detail=f"{kind} is {risk} risk and within limits",
            risk=risk,
        )
