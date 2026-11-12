"""The incident pipeline: alert -> evidence -> diagnosis -> validation -> policy ->
action -> verification.

Webhooks only create or update incident records and enqueue work; a bounded
queue and a small worker pool do the rest, so a burst of alerts can never
block Alertmanager. All state lives in the store: after a restart the agent
re-adopts open incidents instead of forgetting them.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

import structlog

from agent.adapters.sqlite_store import SqliteStore
from agent.app.config import AgentMode, AgentSettings
from agent.app.domain import (
    EXECUTABLE_ACTIONS,
    TERMINAL_STATES,
    ActionResult,
    ActionType,
    Decision,
    DeployEvent,
    Incident,
    PolicyDecision,
    ProposedAction,
    State,
)
from agent.app.metrics import AgentMetrics
from agent.app.models import AmAlert, AmWebhook
from agent.app.state_machine import check_transition
from agent.diagnosis.engine import Diagnoser
from agent.diagnosis.validator import validate
from agent.evidence.collector import EvidenceCollector, utc_now
from agent.policy.engine import PolicyEngine
from agent.policy.limits import record_execution
from agent.ports.protocols import ActionExecutor, Notifier
from agent.verify.verifier import Verifier
from libs.core.cmdb import Cmdb
from services.kit.logging import get_logger

T = TypeVar("T")
Clock = Callable[[], datetime]

JOB_PROCESS = "process"
JOB_EXECUTE = "execute"
JOB_VERIFY = "verify"
APPROVAL_SWEEP_S = 5.0
ACTOR = "agent"


class QueueFullError(Exception):
    """The work queue is full; the caller should answer 429 so the sender retries."""


class ApprovalError(Exception):
    """An approval or rejection that cannot be applied (wrong state, unknown incident)."""


class IncidentPipeline:
    def __init__(
        self,
        *,
        settings: AgentSettings,
        cmdb: Cmdb,
        store: SqliteStore,
        collector: EvidenceCollector,
        diagnoser: Diagnoser,
        policy: PolicyEngine,
        executor: ActionExecutor,
        verifier: Verifier,
        notifier: Notifier,
        metrics: AgentMetrics,
        clock: Clock = utc_now,
    ) -> None:
        self._settings = settings
        self._cmdb = cmdb
        self._store = store
        self._collector = collector
        self._diagnoser = diagnoser
        self._policy = policy
        self._executor = executor
        self._verifier = verifier
        self._notifier = notifier
        self._metrics = metrics
        self._clock = clock
        self._mode: AgentMode = settings.agent_mode
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(settings.agent_queue_size)
        self._intake = asyncio.Lock()
        self._service_locks: dict[str, asyncio.Lock] = {}
        self._tasks: list[asyncio.Task[None]] = []
        # Incidents this process has queued or handled; everything else found
        # open at start-up was left behind by a previous process.
        self._seen: set[str] = set()
        self._adopted = False
        metrics.set_mode(self._mode)

    # --- small helpers ----------------------------------------------------------

    async def _db(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run a store call in the thread pool."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    @property
    def mode(self) -> AgentMode:
        return self._mode

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    def _lock_for(self, service: str) -> asyncio.Lock:
        return self._service_locks.setdefault(service, asyncio.Lock())

    async def _event(
        self, incident: Incident, kind: str, message: str, data: dict[str, Any] | None = None
    ) -> None:
        now = self._clock()
        await self._db(
            self._store.add_event,
            incident.id,
            state=incident.state,
            kind=kind,
            message=message,
            data=data,
            ts=now,
        )
        await self._notifier.publish(
            "timeline",
            {
                "incident_id": incident.id,
                "state": incident.state.value,
                "event_kind": kind,
                "message": message,
                "data": data or {},
                "ts": now.isoformat(),
            },
        )

    async def _transition(
        self, incident: Incident, new: State, message: str, data: dict[str, Any] | None = None
    ) -> None:
        check_transition(incident.state, new)
        incident.state = new
        incident.updated_at = self._clock()
        await self._db(self._store.save_incident, incident)
        await self._event(incident, "state", message, data)
        if new in TERMINAL_STATES:
            self._metrics.incidents.labels(status=new.value).inc()
            self._metrics.incident_duration.observe(
                (incident.updated_at - incident.opened_at).total_seconds()
            )

    async def _audit(
        self, action: str, subject: str, data: dict[str, Any], actor: str = ACTOR
    ) -> None:
        await self._db(
            self._store.audit,
            actor=actor,
            action=action,
            subject=subject,
            data=data,
            ts=self._clock(),
        )

    async def _ticket(
        self, incident: Incident, *, status: str, priority: str, summary: str
    ) -> None:
        runbook = incident.slo or ""
        await self._db(
            self._store.upsert_ticket,
            key=f"incident:{incident.id}",
            severity=incident.severity,
            priority=priority,
            service=incident.service,
            slo=incident.slo,
            status=status,
            summary=summary,
            runbook=runbook,
            incident_id=incident.id,
            links={"incident": f"/incidents/{incident.id}"},
            now=self._clock(),
        )

    async def _close(self, incident: Incident, resolution: str) -> None:
        incident.status = "closed"
        incident.closed_at = self._clock()
        incident.updated_at = incident.closed_at
        incident.resolution = incident.resolution or resolution
        await self._db(self._store.save_incident, incident)
        # Escalated and rejected incidents keep their ticket open: a human owns them.
        if incident.state not in (State.ESCALATED, State.REJECTED):
            await self._db(
                self._store.set_ticket_status, f"incident:{incident.id}", "resolved", self._clock()
            )
        await self._event(incident, "closed", f"incident closed: {incident.resolution}")
        await self._notifier.publish("incident", {"incident_id": incident.id, "status": "closed"})

    async def _close_if_quiet(self, incident: Incident) -> None:
        """Close a finished incident once none of its alerts is firing any more."""
        finished = incident.status == "open" and incident.state in TERMINAL_STATES
        if finished and await self._db(self._store.firing_alerts, incident.id) == 0:
            await self._close(incident, "alerts_resolved")

    async def _escalate(self, incident: Incident, reason: str, detail: str = "") -> None:
        """Hand the incident to a human: terminal state plus a high-priority ticket."""
        incident.escalation_reason = reason
        await self._transition(
            incident, State.ESCALATED, f"escalated: {reason}", {"reason": reason, "detail": detail}
        )
        self._metrics.escalations.labels(reason=reason.split(":", 1)[0]).inc()
        await self._ticket(
            incident,
            status="open",
            priority="high",
            summary=f"ESCALATED ({reason}): {incident.summary}"[:300],
        )
        await self._audit("escalated", incident.id, {"reason": reason, "detail": detail})
        await self._close_if_quiet(incident)

    # --- intake -----------------------------------------------------------------

    async def handle_page(self, notification: AmWebhook) -> None:
        """Record page alerts and enqueue new incidents. Idempotent per alert fingerprint."""
        async with self._intake:
            firing = [a for a in notification.alerts if a.status == "firing"]
            new_keys = set()
            for alert in firing:
                if await self._find_incident(alert) is None:
                    new_keys.add((alert.labels.get("service", "unknown"), alert.labels.get("slo")))
            if self._queue.qsize() + len(new_keys) > self._queue.maxsize:
                self._metrics.queue_rejected.inc()
                raise QueueFullError
            for alert in notification.alerts:
                await self._ingest(alert)
        self._metrics.queue_depth.set(self._queue.qsize())

    async def _find_incident(self, alert: AmAlert) -> Incident | None:
        by_fingerprint = await self._db(
            self._store.open_incident_for_fingerprint, alert.fingerprint
        )
        if by_fingerprint:
            return by_fingerprint
        return await self._db(
            self._store.find_open_incident,
            alert.labels.get("service", "unknown"),
            alert.labels.get("slo"),
        )

    async def _ingest(self, alert: AmAlert) -> None:
        now = self._clock()
        incident = await self._find_incident(alert)
        if incident is None:
            if alert.status != "firing":
                return
            if await self._is_late_notification(alert):
                return
            incident = await self._db(
                self._store.create_incident,
                service=alert.labels.get("service", "unknown"),
                slo=alert.labels.get("slo"),
                severity=alert.labels.get("severity", "page"),
                mode=self._mode,
                diagnosis_mode=self._settings.diagnosis_mode,
                summary=alert.annotations.get("summary", alert.name)[:300],
                now=now,
            )
            await self._db(self._store.upsert_alert, incident.id, alert, now)
            await self._event(
                incident,
                "state",
                f"incident opened by {alert.name}",
                {"alert": alert.name, "fingerprint": alert.fingerprint, "mode": self._mode},
            )
            await self._ticket(incident, status="open", priority="normal", summary=incident.summary)
            await self._notifier.publish(
                "incident",
                {"incident_id": incident.id, "status": "open", "service": incident.service},
            )
            self._seen.add(incident.id)
            self._queue.put_nowait((JOB_PROCESS, incident.id))
            return

        change = await self._db(self._store.upsert_alert, incident.id, alert, now)
        if change != "duplicate":
            await self._event(
                incident,
                "alert",
                f"alert {alert.name} {alert.status}",
                {"alert": alert.name, "status": alert.status, "fingerprint": alert.fingerprint},
            )
        if alert.status == "resolved":
            await self._close_if_quiet(incident)

    async def _is_late_notification(self, alert: AmAlert) -> bool:
        """True for an alert episode that began before its incident was closed.

        Slow burn windows keep an alert firing for a while after recovery has
        been verified. A re-notification of that same episode must not open a
        new incident; a new episode (a later `startsAt`) must.
        """
        closed = await self._db(self._store.last_closed_incident_for_fingerprint, alert.fingerprint)
        if closed is None or closed.closed_at is None or alert.starts_at > closed.closed_at:
            return False
        change = await self._db(self._store.upsert_alert, closed.id, alert, self._clock())
        if change != "duplicate":
            await self._event(
                closed,
                "alert",
                f"alert {alert.name} still firing after closure (same episode); no new incident",
                {"alert": alert.name, "fingerprint": alert.fingerprint},
            )
        return True

    async def handle_ticket(self, notification: AmWebhook) -> None:
        """Ticket-severity alerts become ticket records only; nothing is remediated."""
        now = self._clock()
        for alert in notification.alerts:
            await self._db(
                self._store.upsert_ticket,
                key=f"alert:{alert.fingerprint}",
                severity="ticket",
                priority="normal",
                service=alert.labels.get("service", "unknown"),
                slo=alert.labels.get("slo"),
                status="open" if alert.status == "firing" else "resolved",
                summary=alert.annotations.get("summary", alert.name)[:300],
                runbook=alert.annotations.get("runbook_url", "").rsplit("/", 1)[-1],
                incident_id=None,
                links={"dashboard": alert.annotations.get("dashboard_url", "")},
                now=now,
            )

    # --- workers ----------------------------------------------------------------

    async def start(self) -> None:
        """Start the workers and the approval sweeper, then re-adopt open incidents."""
        self._tasks = [
            asyncio.create_task(self._worker(index))
            for index in range(self._settings.agent_workers)
        ]
        self._tasks.append(asyncio.create_task(self._approval_sweeper()))
        await self._readopt()

    async def stop(self) -> None:
        """Cancel workers. Unfinished incidents stay in the store and are re-adopted."""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    async def drain(self) -> None:
        """Wait until every queued job has been processed (used by tests)."""
        await self._queue.join()

    async def _worker(self, index: int) -> None:
        while True:
            job, incident_id = await self._queue.get()
            self._metrics.queue_depth.set(self._queue.qsize())
            try:
                with structlog.contextvars.bound_contextvars(incident_id=incident_id, worker=index):
                    await self._run_job(job, incident_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                get_logger().exception("pipeline_error", incident_id=incident_id, job=job)
                await self._fail_safe(incident_id)
            finally:
                self._queue.task_done()

    async def _fail_safe(self, incident_id: str) -> None:
        """An unexpected error must end in an escalation, never in a silent stall."""
        incident = await self._db(self._store.get_incident, incident_id)
        if incident and incident.state not in TERMINAL_STATES:
            with contextlib.suppress(Exception):
                await self._escalate(incident, "pipeline_error", "unexpected error; see agent logs")

    async def _run_job(self, job: str, incident_id: str) -> None:
        incident = await self._db(self._store.get_incident, incident_id)
        if incident is None or incident.state in TERMINAL_STATES:
            return
        if job == JOB_PROCESS:
            if incident.state is State.RECEIVED:
                await self._process(incident, failed_action=None)
        elif job == JOB_EXECUTE:
            await self._execute_approved(incident)
        elif job == JOB_VERIFY:
            await self._verify(incident, await self._last_action(incident))

    async def _readopt(self) -> None:
        """After a restart, pick up what a previous process left behind.

        Untouched incidents are processed, verification is resumed, approvals
        keep waiting. An incident interrupted anywhere else is escalated: its
        action may or may not have run, and guessing would risk running it twice.
        """
        if self._adopted:
            return
        self._adopted = True
        for incident in await self._db(self._store.list_incidents, 500, "open"):
            skip = incident.state in TERMINAL_STATES or incident.state is State.PENDING_APPROVAL
            if skip or incident.id in self._seen:
                continue
            self._seen.add(incident.id)
            if incident.state is State.RECEIVED:
                await self._queue.put((JOB_PROCESS, incident.id))
            elif incident.state is State.VERIFYING:
                await self._event(incident, "note", "agent restarted: resuming verification")
                await self._queue.put((JOB_VERIFY, incident.id))
            else:
                await self._escalate(
                    incident, "agent_restart", f"agent restarted while {incident.state.value}"
                )

    # --- the pipeline -----------------------------------------------------------

    async def _process(self, incident: Incident, failed_action: ProposedAction | None) -> None:
        service = self._cmdb.services.get(incident.service)
        if service is None or not service.managed:
            await self._escalate(incident, "unknown_service", f"{incident.service} is not managed")
            return

        # ENRICHED
        bundle = await self._collector.collect(incident.id, incident.service, incident.slo)
        await self._transition(
            incident,
            State.ENRICHED,
            f"collected {len(bundle.items)} evidence items from {', '.join(bundle.services)}",
            {"items": len(bundle.items), "incomplete": list(bundle.incomplete)},
        )

        # DIAGNOSED
        with self._metrics.diagnosis_duration.labels(mode=incident.diagnosis_mode).time():
            record, bundle = await self._diagnoser.diagnose(bundle, failed_action=failed_action)
        await self._db(self._store.add_evidence, bundle, incident.replans + 1)
        await self._db(self._store.add_diagnosis, incident.id, record, self._clock())
        diagnosis = record.diagnosis
        incident.category = diagnosis.category
        incident.root_cause_service = diagnosis.root_cause_service
        incident.degraded_mode = incident.degraded_mode or record.degraded_mode
        self._metrics.diagnoses.labels(mode=record.mode, category=diagnosis.category.value).inc()
        await self._transition(
            incident,
            State.DIAGNOSED,
            f"{diagnosis.category.value} at {diagnosis.root_cause_service} "
            f"(confidence {diagnosis.confidence:.2f}, {record.mode})",
            {
                "category": diagnosis.category.value,
                "root_cause_service": diagnosis.root_cause_service,
                "confidence": diagnosis.confidence,
                "mode": record.mode,
                "degraded_mode": record.degraded_mode,
                "proposed_action": diagnosis.action.type.value,
                "cited": [c.id for c in diagnosis.evidence],
            },
        )

        if self._mode == "observe":
            await self._transition(incident, State.OBSERVED, "observe mode: diagnosis only")
            await self._close_if_quiet(incident)
            return

        # VALIDATED
        result = validate(diagnosis, bundle, self._cmdb, self._settings)
        if not result.ok or result.action is None:
            self._metrics.validation_failures.labels(reason=result.reason).inc()
            self._metrics.actions.labels(
                action=diagnosis.action.type.value, outcome="blocked"
            ).inc()
            await self._audit(
                "validation_rejected",
                incident.id,
                {
                    "reason": result.reason,
                    "detail": result.detail,
                    "action": diagnosis.action.model_dump(mode="json"),
                },
            )
            await self._event(
                incident,
                "validation",
                f"proposal rejected by the validator: {result.reason}",
                {"reason": result.reason, "detail": result.detail},
            )
            await self._escalate(incident, f"validation:{result.reason}", result.detail)
            return
        action = result.action
        await self._transition(
            incident,
            State.VALIDATED,
            f"proposal valid: {action.type.value} on {action.target_service}",
        )

        if action.type not in EXECUTABLE_ACTIONS:
            await self._escalate(incident, "diagnosis_escalated", diagnosis.rationale)
            return
        if failed_action and (action.type, action.target_service) == (
            failed_action.type,
            failed_action.target_service,
        ):
            await self._escalate(
                incident,
                "replan_same_action",
                "the re-plan proposed the action that already failed",
            )
            return

        await self._decide(incident, action)

    async def _decide(self, incident: Incident, action: ProposedAction) -> None:
        """POLICY_DECIDED and what follows from the decision."""
        now = self._clock()
        decision = await self._db(
            self._policy.evaluate, action, incident_id=incident.id, mode=self._mode, now=now
        )
        kind = action.type.value
        status = {
            Decision.ALLOW: "approved",
            Decision.REQUIRE_APPROVAL: "pending_approval",
            Decision.DRY_RUN: "dry_run",
            Decision.DENY: "blocked",
            Decision.COVERED: "covered",
        }[decision.decision]
        action_id = await self._db(
            self._store.add_action,
            incident.id,
            action_type=kind,
            target=action.target_service,
            params=action.params,
            risk=decision.risk,
            decision=decision.decision.value,
            rule=decision.rule,
            dry_run=decision.decision is Decision.DRY_RUN,
            status=status,
            ts=now,
        )
        await self._audit(
            "policy_decision",
            incident.id,
            {
                "action": action.model_dump(mode="json"),
                "decision": decision.decision.value,
                "rule": decision.rule,
                "detail": decision.detail,
                "mode": self._mode,
            },
        )
        await self._transition(
            incident,
            State.POLICY_DECIDED,
            f"policy: {decision.decision.value} ({decision.rule})",
            {
                "decision": decision.decision.value,
                "rule": decision.rule,
                "detail": decision.detail,
                "risk": decision.risk,
                "action": kind,
                "target": action.target_service,
                "params": action.params,
            },
        )
        await self._notifier.publish(
            "action",
            {"incident_id": incident.id, "action": kind, "status": status, "rule": decision.rule},
        )
        await self._apply_decision(incident, action, action_id, decision)

    async def _apply_decision(
        self, incident: Incident, action: ProposedAction, action_id: int, decision: PolicyDecision
    ) -> None:
        kind = action.type.value
        if decision.decision is Decision.DENY:
            self._metrics.actions.labels(action=kind, outcome="blocked").inc()
            await self._escalate(incident, f"policy:{decision.rule}", decision.detail)
        elif decision.decision is Decision.DRY_RUN:
            self._metrics.actions.labels(action=kind, outcome="dry_run").inc()
            await self._transition(
                incident,
                State.DRY_RUN_COMPLETE,
                f"dry run: {decision.detail}; nothing was executed",
            )
            await self._close_if_quiet(incident)
        elif decision.decision is Decision.REQUIRE_APPROVAL:
            self._metrics.actions.labels(action=kind, outcome="pending_approval").inc()
            await self._transition(
                incident,
                State.PENDING_APPROVAL,
                f"waiting for human approval of {kind} on {action.target_service}",
                {"action_id": action_id, "timeout_s": self._policy.policy.approval.timeout_s},
            )
            await self._notifier.publish("approval", {"incident_id": incident.id, "action": kind})
        elif decision.decision is Decision.COVERED:
            await self._transition(
                incident,
                State.VERIFYING,
                f"{decision.detail}; verifying recovery instead of repeating the action",
                {"covered_by": decision.covered_by},
            )
            await self._verify(incident, action)
        else:
            await self._execute(incident, action, action_id)

    # --- approval ---------------------------------------------------------------

    async def _pending_action(self, incident_id: str) -> dict[str, Any] | None:
        actions = await self._db(self._store.actions, incident_id)
        pending = [a for a in actions if a["status"] == "pending_approval"]
        return pending[-1] if pending else None

    async def approve(self, incident_id: str, actor: str) -> Incident:
        """Approve the pending high-risk action; a worker then executes it."""
        incident = await self._db(self._store.get_incident, incident_id)
        if incident is None:
            raise ApprovalError("unknown incident")
        pending = await self._pending_action(incident_id)
        if incident.state is not State.PENDING_APPROVAL or pending is None:
            raise ApprovalError(f"incident is {incident.state.value}, nothing awaits approval")
        if self._queue.full():
            raise QueueFullError
        await self._db(
            self._store.update_action, pending["id"], status="approved", decided_by=actor
        )
        await self._audit(
            "action_approved",
            incident_id,
            {"action_id": pending["id"], "type": pending["type"]},
            actor,
        )
        await self._event(incident, "approval", f"{pending['type']} approved by {actor}")
        self._queue.put_nowait((JOB_EXECUTE, incident_id))
        return incident

    async def reject(self, incident_id: str, actor: str, reason: str = "") -> Incident:
        incident = await self._db(self._store.get_incident, incident_id)
        if incident is None:
            raise ApprovalError("unknown incident")
        pending = await self._pending_action(incident_id)
        if incident.state is not State.PENDING_APPROVAL or pending is None:
            raise ApprovalError(f"incident is {incident.state.value}, nothing awaits approval")
        await self._db(
            self._store.update_action, pending["id"], status="rejected", decided_by=actor
        )
        self._metrics.actions.labels(action=pending["type"], outcome="rejected").inc()
        await self._audit(
            "action_rejected", incident_id, {"action_id": pending["id"], "reason": reason}, actor
        )
        incident.resolution = "action_rejected"
        await self._transition(
            incident, State.REJECTED, f"{pending['type']} rejected by {actor}", {"reason": reason}
        )
        await self._ticket(
            incident,
            status="open",
            priority="high",
            summary=f"REJECTED ({pending['type']}): {incident.summary}"[:300],
        )
        await self._close_if_quiet(incident)
        return incident

    async def _execute_approved(self, incident: Incident) -> None:
        actions = await self._db(self._store.actions, incident.id)
        approved = [a for a in actions if a["status"] == "approved"]
        if incident.state is not State.PENDING_APPROVAL or not approved:
            return
        row = approved[-1]
        action = ProposedAction(
            type=ActionType(row["type"]), target_service=row["target"], params=row["params"]
        )
        await self._execute(incident, action, row["id"])

    async def _approval_sweeper(self) -> None:
        """Escalate approvals nobody answered within the policy's timeout."""
        timeout = self._policy.policy.approval.timeout_s
        while True:
            await asyncio.sleep(APPROVAL_SWEEP_S)
            with contextlib.suppress(Exception):
                await self.expire_approvals(timeout)

    async def expire_approvals(self, timeout_s: float) -> int:
        expired = 0
        now = self._clock()
        for row in await self._db(self._store.pending_actions):
            age = (now - datetime.fromisoformat(row["requested_at"])).total_seconds()
            incident = await self._db(self._store.get_incident, row["incident_id"])
            if age < timeout_s or incident is None or incident.state is not State.PENDING_APPROVAL:
                continue
            await self._db(self._store.update_action, row["id"], status="expired")
            await self._escalate(
                incident, "approval_timeout", f"no decision within {timeout_s:.0f}s"
            )
            expired += 1
        return expired

    # --- action and verification --------------------------------------------------

    async def _run_action(self, action: ProposedAction) -> ActionResult:
        target = action.target_service
        if action.type is ActionType.RESTART:
            return await self._executor.restart(target)
        if action.type is ActionType.SCALE:
            return await self._executor.scale(target, int(action.params["replicas"]))
        if action.type is ActionType.ROLLBACK:
            return await self._executor.rollback(target, str(action.params["to_version"]))
        return ActionResult(success=False, detail=f"{action.type.value} is not executable")

    async def _execute(self, incident: Incident, action: ProposedAction, action_id: int) -> None:
        kind, target = action.type.value, action.target_service
        # At most one remediation in flight per service.
        async with self._lock_for(target):
            await self._transition(
                incident, State.ACTING, f"executing {kind} on {target}", {"params": action.params}
            )
            started = self._clock()
            await self._db(
                self._store.update_action, action_id, status="executing", started_at=started
            )
            try:
                result = await self._run_action(action)
            except Exception as exc:
                result = ActionResult(success=False, detail=f"{type(exc).__name__}: {exc}")
            finished = self._clock()
            await self._db(
                self._store.update_action,
                action_id,
                status="executed" if result.success else "failed",
                finished_at=finished,
                outcome=result.model_dump(mode="json"),
            )
            await self._audit(
                "action_executed" if result.success else "action_failed",
                incident.id,
                {
                    "action": action.model_dump(mode="json"),
                    "result": result.model_dump(mode="json"),
                },
            )
            if result.success:
                await self._db(
                    record_execution,
                    self._store,
                    action=kind,
                    target=target,
                    incident_id=incident.id,
                    now=finished,
                )
                if action.type is ActionType.ROLLBACK:
                    await self._record_rollback(incident, action, finished)
        await self._event(
            incident,
            "action",
            f"{kind} {'succeeded' if result.success else 'failed'}: {result.detail}"[:500],
            {"success": result.success, "duration_s": result.duration_s, "data": result.data},
        )
        await self._notifier.publish(
            "action",
            {
                "incident_id": incident.id,
                "action": kind,
                "status": "executed" if result.success else "failed",
            },
        )
        if not result.success:
            self._metrics.actions.labels(action=kind, outcome="failed").inc()
            await self._escalate(incident, "action_failed", result.detail)
            return
        self._metrics.actions.labels(action=kind, outcome="executed").inc()
        await self._cover_pending_siblings(incident, action)
        await self._transition(incident, State.VERIFYING, f"verifying recovery after {kind}")
        await self._verify(incident, action)

    async def _cover_pending_siblings(self, incident: Incident, action: ProposedAction) -> None:
        """Other incidents waiting for approval of the action that just ran are covered by it.

        One root cause often pages for several SLOs. A human approves once; the
        siblings then verify their own recovery instead of asking again.
        """
        for row in await self._db(self._store.pending_actions):
            same = (row["type"], row["target"]) == (action.type.value, action.target_service)
            if not same or row["incident_id"] == incident.id:
                continue
            sibling = await self._db(self._store.get_incident, row["incident_id"])
            if sibling is None or sibling.state is not State.PENDING_APPROVAL:
                continue
            await self._db(self._store.update_action, row["id"], status="covered")
            await self._audit(
                "action_covered", sibling.id, {"action_id": row["id"], "covered_by": incident.id}
            )
            await self._transition(
                sibling,
                State.VERIFYING,
                f"{row['type']} on {row['target']} was executed for {incident.id}; "
                "verifying recovery instead of waiting for a second approval",
                {"covered_by": incident.id},
            )
            self._seen.add(sibling.id)
            await self._queue.put((JOB_VERIFY, sibling.id))

    async def _record_rollback(
        self, incident: Incident, action: ProposedAction, now: datetime
    ) -> None:
        history = await self._db(self._store.deploys, action.target_service, 1)
        await self._db(
            self._store.add_deploy,
            DeployEvent(
                service=action.target_service,
                version=str(action.params["to_version"]),
                previous_version=history[0].version if history else None,
                status="completed",
                reason="rollback",
                incident_id=incident.id,
                actor=ACTOR,
                ts=now,
            ),
        )
        await self._notifier.publish(
            "deploy",
            {
                "service": action.target_service,
                "version": action.params["to_version"],
                "reason": "rollback",
            },
        )

    async def _last_action(self, incident: Incident) -> ProposedAction | None:
        actions = await self._db(self._store.actions, incident.id)
        rows = [a for a in actions if a["status"] in ("executed", "covered")]
        if not rows:
            return None
        row = rows[-1]
        return ProposedAction(
            type=ActionType(row["type"]), target_service=row["target"], params=row["params"]
        )

    async def _verify(self, incident: Incident, action: ProposedAction | None) -> None:
        target = (
            action.target_service if action else (incident.root_cause_service or incident.service)
        )
        result = await self._verifier.verify(incident, target, incident.category)
        self._metrics.recovery_verified.labels(
            result="recovered" if result.recovered else "failed"
        ).inc()
        data = {"checks": result.checks, "duration_s": result.duration_s, "detail": result.detail}
        if result.recovered:
            incident.resolution = "verified_recovery"
            await self._transition(
                incident, State.RESOLVED, f"recovery verified: {result.detail}", data
            )
            await self._close(incident, "verified_recovery")
            return
        await self._transition(
            incident, State.FAILED_VERIFICATION, f"verification failed: {result.detail}", data
        )
        if action is not None and incident.replans < self._settings.max_replans:
            incident.replans += 1
            await self._event(
                incident,
                "replan",
                f"re-planning after {action.type.value} did not fix the problem "
                f"({incident.replans}/{self._settings.max_replans})",
            )
            await self._process(incident, failed_action=action)
            return
        await self._escalate(incident, "verification_failed", result.detail)

    # --- administration ---------------------------------------------------------

    async def set_mode(self, mode: AgentMode, actor: str) -> None:
        """Change the runtime mode (the kill switch). Audited; lasts until restart."""
        previous = self._mode
        self._mode = mode
        self._metrics.set_mode(mode)
        await self._db(self._store.set_state, "mode", mode)
        await self._audit("mode_changed", "agent", {"from": previous, "to": mode}, actor)
        await self._notifier.publish("mode", {"from": previous, "to": mode, "actor": actor})

    async def record_deploy(self, event: DeployEvent) -> None:
        await self._db(self._store.add_deploy, event)
        await self._audit(
            "deploy_recorded",
            event.service,
            {
                "version": event.version,
                "previous_version": event.previous_version,
                "status": event.status,
                "override_reason": event.override_reason,
            },
            event.actor,
        )
        await self._notifier.publish(
            "deploy", {"service": event.service, "version": event.version, "status": event.status}
        )
