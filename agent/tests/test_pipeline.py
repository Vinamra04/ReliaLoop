from datetime import timedelta
from pathlib import Path

import pytest

from agent.app.domain import (
    ActionResult,
    ActionType,
    Category,
    Citation,
    Diagnosis,
    DiagnosisRecord,
    EvidenceBundle,
    ProposedAction,
    State,
)
from agent.app.models import AmWebhook
from agent.app.pipeline import ApprovalError, QueueFullError
from agent.ports.protocols import SourceError
from agent.tests.fixtures import alert, webhook
from agent.tests.rig import PipelineRig, gateway_alert, orders_alert

HAPPY_PATH = [
    "RECEIVED",
    "ENRICHED",
    "DIAGNOSED",
    "VALIDATED",
    "POLICY_DECIDED",
    "ACTING",
    "VERIFYING",
    "RESOLVED",
]


class ScriptedDiagnoser:
    """Returns prepared diagnoses in order (stands in for a misbehaving LLM)."""

    def __init__(self, *diagnoses: Diagnosis) -> None:
        self.queue = list(diagnoses)
        self.failed_actions: list[ProposedAction | None] = []

    async def diagnose(self, bundle: EvidenceBundle, *, failed_action=None):
        self.failed_actions.append(failed_action)
        return DiagnosisRecord(diagnosis=self.queue.pop(0), mode="llm", provider="fake"), bundle


def proposal(
    action: ActionType,
    target: str = "orders",
    *,
    category: Category = Category.ERROR_SPIKE,
    cite: str = "err_ratio_1m@orders",
    params: dict | None = None,
    confidence: float = 0.9,
) -> Diagnosis:
    return Diagnosis(
        category=category,
        root_cause_service=target,
        confidence=confidence,
        evidence=(Citation(id=cite, observation="observed"),),
        action=ProposedAction(type=action, target_service=target, params=params or {}),
        rationale="scripted",
    )


# --- enforce: low-risk actions run without a human --------------------------------


async def test_error_spike_is_detected_diagnosed_remediated_and_verified(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    rig.error_spike()
    await rig.run(gateway_alert())

    incident = rig.incident()
    assert incident.id == "INC-20261112-001"
    assert rig.states(incident.id) == HAPPY_PATH
    assert (incident.state, incident.status) == (State.RESOLVED, "closed")
    assert (incident.category, incident.root_cause_service) == (Category.ERROR_SPIKE, "orders")
    assert incident.resolution == "verified_recovery"
    assert rig.executor.calls == [("restart", "orders", None)]

    action = rig.store.actions(incident.id)[0]
    assert (action["type"], action["target"], action["status"]) == (
        "restart_service",
        "orders",
        "executed",
    )
    assert (action["decision"], action["rule"], action["risk"]) == ("allow", "low_risk_auto", "low")
    assert action["outcome"]["success"] is True
    assert rig.store.diagnoses(incident.id)[0]["mode"] == "rules"
    assert rig.store.evidence(incident.id)[0]["bundle"]["services"] == ["gateway", "orders"]
    assert rig.store.tickets()[0]["status"] == "resolved"
    assert rig.store.verify_audit_chain()[0] is True
    assert [e["action"] for e in rig.store.audit_entries(incident.id)] == [
        "policy_decision",
        "action_executed",
    ]
    assert rig.metric("agent_actions_total", action="restart_service", outcome="executed") == 1
    assert rig.metric("agent_recovery_verified_total", result="recovered") == 1
    assert rig.metric("agent_diagnosis_total", category="ERROR_SPIKE", mode="rules") == 1
    assert rig.metric("agent_incidents_total", status="RESOLVED") == 1
    assert rig.metric("agent_mode", mode="enforce") == 1


async def test_saturation_scales_orders_by_one(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    rig.saturation()
    await rig.run(gateway_alert(slo="gateway-latency"))
    assert rig.incident().state is State.RESOLVED
    assert rig.incident().category is Category.LOAD_SATURATION
    assert rig.executor.calls == [("scale", "orders", 2)]


async def test_memory_leak_page_without_slo_restarts_the_gateway(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    rig.memory_leak("gateway")
    rig.metrics.set("rss_pct_of_limit", "gateway", 0.75)
    page = alert("MemoryExhaustionPredicted", service="gateway", slo=None, fingerprint="fp-mem")

    async def restart_frees_memory(service: str) -> ActionResult:
        rig.executor.calls.append(("restart", service, None))
        rig.metrics.set("rss_pct_of_limit", "gateway", 0.3)
        return ActionResult(success=True, detail="restarted")

    rig.executor.restart = restart_frees_memory  # type: ignore[method-assign]
    await rig.run(page)
    incident = rig.incident()
    assert incident.slo is None
    assert (incident.state, incident.category) == (State.RESOLVED, Category.MEMORY_LEAK)
    assert rig.executor.calls == [("restart", "gateway", None)]


# --- high-risk actions wait for a human -------------------------------------------


async def test_rollback_waits_for_approval_then_executes(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    rig.bad_deploy()
    await rig.run(gateway_alert())

    incident = rig.incident()
    assert incident.state is State.PENDING_APPROVAL
    assert rig.executor.calls == []
    pending = rig.store.pending_actions()
    assert len(pending) == 1
    assert (pending[0]["type"], pending[0]["params"], pending[0]["risk"]) == (
        "rollback_service",
        {"to_version": "v1"},
        "high",
    )
    assert (
        rig.metric("agent_actions_total", action="rollback_service", outcome="pending_approval")
        == 1
    )

    await rig.pipeline.approve(incident.id, "alice")
    await rig.settle()

    incident = rig.incident()
    assert incident.state is State.RESOLVED
    assert rig.executor.calls == [("rollback", "orders", "v1")]
    assert rig.states(incident.id)[-4:] == ["PENDING_APPROVAL", "ACTING", "VERIFYING", "RESOLVED"]
    rollback = rig.store.deploys("orders")[0]
    assert (rollback.version, rollback.previous_version, rollback.reason, rollback.incident_id) == (
        "v1",
        "v2-bad",
        "rollback",
        incident.id,
    )
    audit = {e["action"]: e["actor"] for e in rig.store.audit_entries(incident.id)}
    assert audit["action_approved"] == "alice"
    assert rig.store.actions(incident.id)[0]["decided_by"] == "alice"


async def test_crash_loop_after_deploy_also_needs_approval(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    rig.crash_loop()
    await rig.run(gateway_alert())
    assert rig.incident().state is State.PENDING_APPROVAL
    assert rig.incident().category is Category.CRASH_LOOP
    assert rig.executor.calls == []


async def test_rejected_rollback_executes_nothing(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    rig.bad_deploy()
    await rig.run(gateway_alert())
    await rig.pipeline.reject(rig.incident().id, "bob", "not during the release freeze")

    incident = rig.incident()
    assert incident.state is State.REJECTED
    assert rig.executor.calls == []
    assert rig.store.actions(incident.id)[0]["status"] == "rejected"
    assert rig.store.tickets()[0]["priority"] == "high"
    assert rig.metric("agent_actions_total", action="rollback_service", outcome="rejected") == 1
    rejected = [e for e in rig.store.audit_entries(incident.id) if e["action"] == "action_rejected"]
    assert rejected[0]["data"]["reason"] == "not during the release freeze"


async def test_approval_errors(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    rig.error_spike()
    await rig.run(gateway_alert())
    with pytest.raises(ApprovalError, match="unknown incident"):
        await rig.pipeline.approve("INC-nope", "alice")
    with pytest.raises(ApprovalError, match="nothing awaits approval"):
        await rig.pipeline.approve(rig.incident().id, "alice")
    with pytest.raises(ApprovalError, match="nothing awaits approval"):
        await rig.pipeline.reject(rig.incident().id, "alice")


async def test_unanswered_approval_escalates_after_the_timeout(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    rig.bad_deploy()
    await rig.run(gateway_alert())
    assert await rig.pipeline.expire_approvals(600) == 0
    rig.clock.advance(601)
    assert await rig.pipeline.expire_approvals(600) == 1

    incident = rig.incident()
    assert (incident.state, incident.escalation_reason) == (State.ESCALATED, "approval_timeout")
    assert rig.executor.calls == []
    assert rig.store.pending_actions() == []


# --- modes ------------------------------------------------------------------------


async def test_dry_run_records_intent_and_executes_nothing(tmp_path: Path):
    rig = PipelineRig(tmp_path, agent_mode="dry_run")
    rig.error_spike()
    await rig.run(gateway_alert())

    incident = rig.incident()
    assert rig.states(incident.id)[-1] == "DRY_RUN_COMPLETE"
    assert rig.executor.calls == []
    action = rig.store.actions(incident.id)[0]
    assert (action["status"], action["dry_run"], action["decision"]) == ("dry_run", 1, "dry_run")
    assert rig.metric("agent_actions_total", action="restart_service", outcome="dry_run") == 1
    assert incident.status == "open"  # the alert is still firing


async def test_dry_run_rollback_does_not_ask_for_approval(tmp_path: Path):
    rig = PipelineRig(tmp_path, agent_mode="dry_run")
    rig.bad_deploy()
    await rig.run(gateway_alert())
    assert rig.incident().state is State.DRY_RUN_COMPLETE
    assert rig.store.pending_actions() == []


async def test_observe_mode_only_diagnoses(tmp_path: Path):
    rig = PipelineRig(tmp_path, agent_mode="observe")
    rig.error_spike()
    await rig.run(gateway_alert())
    incident = rig.incident()
    assert rig.states(incident.id) == ["RECEIVED", "ENRICHED", "DIAGNOSED", "OBSERVED"]
    assert incident.category is Category.ERROR_SPIKE
    assert rig.store.actions(incident.id) == []
    assert rig.executor.calls == []


async def test_kill_switch_changes_mode_at_runtime_and_is_audited(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    await rig.pipeline.set_mode("observe", "carol")
    rig.error_spike()
    await rig.run(gateway_alert())
    assert rig.incident().state is State.OBSERVED
    assert rig.executor.calls == []
    entry = rig.store.audit_entries("agent")[0]
    assert (entry["action"], entry["actor"], entry["data"]) == (
        "mode_changed",
        "carol",
        {"from": "enforce", "to": "observe"},
    )
    assert rig.metric("agent_mode", mode="observe") == 1
    assert rig.metric("agent_mode", mode="enforce") == 0


# --- validator and policy block unsafe proposals ----------------------------------


@pytest.mark.parametrize(
    ("diagnosis", "reason"),
    [
        (proposal(ActionType.RESTART, cite="invented_metric@orders"), "unknown_evidence_id"),
        (proposal(ActionType.RESTART, "postgres"), "unmanaged_target"),
        (
            proposal(
                ActionType.SCALE,
                "gateway",
                category=Category.LOAD_SATURATION,
                cite="err_ratio_1m@gateway",
            ),
            "action_not_allowed",
        ),
        (proposal(ActionType.RESTART, confidence=0.4), "low_confidence"),
        (
            proposal(ActionType.ROLLBACK, category=Category.BAD_DEPLOY),
            "rollback_without_recent_deploy",
        ),
        (
            proposal(ActionType.SCALE, category=Category.ERROR_SPIKE),
            "action_inconsistent_with_category",
        ),
    ],
)
async def test_unsafe_proposals_are_rejected_and_escalated(tmp_path: Path, diagnosis, reason):
    rig = PipelineRig(tmp_path, diagnoser=ScriptedDiagnoser(diagnosis))
    rig.error_spike()
    await rig.run(gateway_alert())

    incident = rig.incident()
    assert incident.state is State.ESCALATED
    assert incident.escalation_reason == f"validation:{reason}"
    assert rig.executor.calls == []
    assert rig.store.actions(incident.id) == []
    assert rig.metric("agent_validation_failures_total", reason=reason) == 1
    assert rig.metric("agent_escalations_total", reason="validation") == 1
    timeline = [e for e in rig.store.events(incident.id) if e["kind"] == "validation"]
    assert timeline[0]["data"]["reason"] == reason
    ticket = rig.store.tickets()[0]
    assert (ticket["priority"], ticket["status"]) == ("high", "open")
    assert "ESCALATED" in ticket["summary"]


async def test_unknown_category_escalates_without_action(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    await rig.run(gateway_alert())  # healthy evidence: no rule matches
    incident = rig.incident()
    assert (incident.state, incident.escalation_reason) == (State.ESCALATED, "diagnosis_escalated")
    assert incident.category is Category.UNKNOWN


async def test_policy_denial_escalates(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    rig.error_spike()
    for fingerprint in ("a", "b", "c"):
        await rig.run(gateway_alert(fingerprint=fingerprint))
        rig.clock.advance(120)

    third = rig.incident(2)
    assert [rig.incident(i).state for i in range(2)] == [State.RESOLVED, State.RESOLVED]
    assert (third.state, third.escalation_reason) == (State.ESCALATED, "policy:restart_limit")
    assert len(rig.executor.calls) == 2
    assert rig.store.actions(third.id)[0]["status"] == "blocked"
    assert rig.metric("agent_actions_total", action="restart_service", outcome="blocked") == 1


# --- correlation, idempotency and lifecycle ---------------------------------------


async def test_two_pages_with_one_root_cause_trigger_one_action(tmp_path: Path):
    rig = PipelineRig(tmp_path, agent_workers=1)
    rig.error_spike()
    await rig.page(orders_alert())
    await rig.page(gateway_alert())
    await rig.settle()

    first, second = rig.incident(0), rig.incident(1)
    assert (first.service, second.service) == ("orders", "gateway")
    assert first.state is State.RESOLVED
    assert second.state is State.RESOLVED
    assert rig.executor.calls == [("restart", "orders", None)]
    covered = rig.store.actions(second.id)[0]
    assert (covered["status"], covered["decision"]) == ("covered", "covered")
    assert "POLICY_DECIDED" in rig.states(second.id)
    assert "ACTING" not in rig.states(second.id)


async def test_repeated_notifications_do_not_create_new_incidents(tmp_path: Path):
    rig = PipelineRig(tmp_path, agent_mode="dry_run")
    rig.error_spike()
    await rig.run(gateway_alert())
    await rig.run(gateway_alert())
    await rig.run(gateway_alert(name="SLOBurnRateSlow", fingerprint="fp-slow"))
    assert len(rig.store.list_incidents()) == 1
    incident = rig.incident()
    assert [a["name"] for a in rig.store.alerts(incident.id)] == [
        "SLOBurnRateFast",
        "SLOBurnRateSlow",
    ]
    assert len([e for e in rig.store.events(incident.id) if e["kind"] == "alert"]) == 1


async def test_resolved_alerts_close_a_finished_incident(tmp_path: Path):
    rig = PipelineRig(tmp_path, agent_mode="dry_run")
    rig.error_spike()
    await rig.run(gateway_alert(), gateway_alert(name="SLOBurnRateSlow", fingerprint="fp-slow"))
    await rig.run(gateway_alert(status="resolved"))
    assert rig.incident().status == "open"  # one alert is still firing
    await rig.run(gateway_alert(name="SLOBurnRateSlow", fingerprint="fp-slow", status="resolved"))

    incident = rig.incident()
    assert (incident.status, incident.resolution) == ("closed", "alerts_resolved")
    assert incident.closed_at is not None
    # A new alert for the same SLO now opens a fresh incident.
    await rig.run(gateway_alert(fingerprint="fp-again"))
    assert len(rig.store.list_incidents()) == 2


async def test_resolved_notification_for_an_unknown_alert_is_ignored(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    await rig.run(gateway_alert(status="resolved"))
    assert rig.store.list_incidents() == []


async def test_incident_for_an_unmanaged_service_is_escalated(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    await rig.run(alert(service="postgres", slo=None, fingerprint="fp-pg"))
    incident = rig.incident()
    assert (incident.state, incident.escalation_reason) == (State.ESCALATED, "unknown_service")


async def test_full_queue_rejects_the_webhook_and_creates_nothing(tmp_path: Path):
    rig = PipelineRig(tmp_path, agent_queue_size=1)
    rig.error_spike()
    await rig.page(gateway_alert())
    with pytest.raises(QueueFullError):
        await rig.page(orders_alert())
    assert len(rig.store.list_incidents()) == 1
    assert rig.metric("agent_queue_rejected_total") == 1
    # A repeat of the queued alert is still accepted: it needs no queue slot.
    await rig.page(gateway_alert())
    await rig.settle()
    assert rig.pipeline.queue_depth == 0


# --- failures -----------------------------------------------------------------------


async def test_failed_action_escalates(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    rig.error_spike()
    rig.executor.results["restart"] = ActionResult(success=False, detail="restart timed out")
    await rig.run(gateway_alert())
    incident = rig.incident()
    assert (incident.state, incident.escalation_reason) == (State.ESCALATED, "action_failed")
    assert rig.store.actions(incident.id)[0]["status"] == "failed"
    assert rig.metric("agent_actions_total", action="restart_service", outcome="failed") == 1
    assert "VERIFYING" not in rig.states(incident.id)


async def test_executor_exception_is_a_failed_action_not_a_crash(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    rig.error_spike()
    rig.executor.raise_on.add("restart")
    await rig.run(gateway_alert())
    assert rig.incident().escalation_reason == "action_failed"


async def test_failed_verification_replans_once_then_escalates(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    rig.error_spike()
    rig.recovered(False)
    await rig.run(gateway_alert())

    incident = rig.incident()
    assert incident.state is State.ESCALATED
    # The rule engine proposes the same restart again, which is refused.
    assert incident.escalation_reason == "replan_same_action"
    assert incident.replans == 1
    assert rig.executor.calls == [("restart", "orders", None)]
    assert "FAILED_VERIFICATION" in rig.states(incident.id)
    assert rig.metric("agent_recovery_verified_total", result="failed") == 1
    assert len(rig.store.diagnoses(incident.id)) == 2


async def test_replan_may_try_one_different_action(tmp_path: Path):
    scale = proposal(
        ActionType.SCALE,
        category=Category.LOAD_SATURATION,
        cite="worker_utilization@orders",
        params={"replicas": 2},
    )
    diagnoser = ScriptedDiagnoser(proposal(ActionType.RESTART), scale)
    rig = PipelineRig(tmp_path, diagnoser=diagnoser)
    rig.error_spike()
    rig.recovered(False)

    async def scale_fixes_it(service: str, replicas: int) -> ActionResult:
        rig.executor.calls.append(("scale", service, replicas))
        rig.recovered(True)
        return ActionResult(success=True, detail="scaled")

    rig.executor.scale = scale_fixes_it  # type: ignore[method-assign]
    # Verification of the first action takes longer than the 60 s cooldown.
    original_verify = rig.verifier.verify

    async def slow_verify(*args, **kwargs):
        rig.clock.advance(140)
        return await original_verify(*args, **kwargs)

    rig.verifier.verify = slow_verify  # type: ignore[method-assign]
    await rig.run(gateway_alert())

    incident = rig.incident()
    assert incident.state is State.RESOLVED
    assert incident.replans == 1
    assert rig.executor.calls == [("restart", "orders", None), ("scale", "orders", 2)]
    assert diagnoser.failed_actions[0] is None
    assert diagnoser.failed_actions[1].type is ActionType.RESTART
    assert len(rig.store.actions(incident.id)) == 2


async def test_unexpected_pipeline_error_escalates_instead_of_stalling(tmp_path: Path):
    rig = PipelineRig(tmp_path)

    async def boom(*args, **kwargs):
        raise RuntimeError("collector bug")

    rig.collector.collect = boom  # type: ignore[method-assign]
    await rig.run(gateway_alert())
    incident = rig.incident()
    assert (incident.state, incident.escalation_reason) == (State.ESCALATED, "pipeline_error")
    assert rig.metric("agent_escalations_total", reason="pipeline_error") == 1


async def test_evidence_source_failure_escalates_safely(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    rig.metrics.fail = True
    await rig.run(gateway_alert())
    incident = rig.incident()
    assert incident.state is State.ESCALATED
    assert rig.store.evidence(incident.id)[0]["bundle"]["incomplete"] == ["prometheus"]
    assert rig.executor.calls == []


# --- restart re-adoption -------------------------------------------------------------


async def test_restart_resumes_verification_and_escalates_interrupted_work(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    rig.error_spike()
    await rig.page(gateway_alert())
    await rig.page(orders_alert())
    verifying, interrupted = rig.incident(1), rig.incident(0)
    # Simulate a crash: one incident was verifying an executed action, one was mid-flight.
    for state in (
        State.ENRICHED,
        State.DIAGNOSED,
        State.VALIDATED,
        State.POLICY_DECIDED,
        State.ACTING,
        State.VERIFYING,
    ):
        verifying.state = state
    verifying.category = Category.ERROR_SPIKE
    rig.store.save_incident(verifying)
    action_id = rig.store.add_action(
        verifying.id,
        action_type="restart_service",
        target="orders",
        params={},
        risk="low",
        decision="allow",
        rule="low_risk_auto",
        dry_run=False,
        status="executed",
        ts=rig.clock.now,
    )
    assert action_id
    interrupted.state = State.ENRICHED
    rig.store.save_incident(interrupted)

    restarted = rig.new_pipeline()
    await restarted.start()
    await restarted.drain()
    await restarted.stop()

    assert rig.store.get_incident(verifying.id).state is State.RESOLVED
    after = rig.store.get_incident(interrupted.id)
    assert (after.state, after.escalation_reason) == (State.ESCALATED, "agent_restart")
    assert rig.executor.calls == []  # nothing was executed twice


async def test_pending_approval_survives_a_restart(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    rig.bad_deploy()
    await rig.run(gateway_alert())
    restarted = rig.new_pipeline()
    await restarted.start()
    await restarted.approve(rig.incident().id, "alice")
    await restarted.drain()
    await restarted.stop()
    assert rig.incident().state is State.RESOLVED
    assert rig.executor.calls == [("rollback", "orders", "v1")]


# --- tickets and deploys -------------------------------------------------------------


async def test_ticket_alerts_create_and_resolve_tickets_without_incidents(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    crash = alert("CrashLoop", severity="ticket", slo=None, service="orders", fingerprint="fp-t")
    crash["annotations"]["runbook_url"] = "http://localhost:8080/runbooks/crash-loop"
    await rig.pipeline.handle_ticket(AmWebhook.model_validate(webhook(crash)))
    ticket = rig.store.tickets()[0]
    assert (ticket["key"], ticket["status"], ticket["runbook"], ticket["service"]) == (
        "alert:fp-t",
        "open",
        "crash-loop",
        "orders",
    )
    resolved = {**crash, "status": "resolved"}
    await rig.pipeline.handle_ticket(AmWebhook.model_validate(webhook(resolved)))
    assert rig.store.tickets()[0]["status"] == "resolved"
    assert rig.store.list_incidents() == []
    assert rig.executor.calls == []


async def test_runtime_source_error_during_status_does_not_break_the_pipeline(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    rig.error_spike()
    rig.executor.fail_status = True
    await rig.run(gateway_alert())
    # Evidence is incomplete (no runtime facts) but the error spike is still diagnosed;
    # verification cannot confirm container health, so the incident escalates.
    incident = rig.incident()
    assert incident.category is Category.ERROR_SPIKE
    assert incident.state is State.ESCALATED
    assert isinstance(SourceError("x"), Exception)


async def test_deploy_events_are_recorded_and_audited(tmp_path: Path):
    from agent.app.domain import DeployEvent

    rig = PipelineRig(tmp_path)
    await rig.pipeline.record_deploy(
        DeployEvent(
            service="orders",
            version="v2-bad",
            previous_version="v1",
            status="completed",
            override_reason="demo",
            ts=rig.clock.now - timedelta(seconds=5),
        )
    )
    assert rig.store.deploys("orders")[0].version == "v2-bad"
    entry = rig.store.audit_entries("orders")[0]
    assert (entry["action"], entry["data"]["override_reason"]) == ("deploy_recorded", "demo")


async def test_late_notification_of_the_same_alert_episode_does_not_reopen(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    rig.error_spike()
    await rig.run(gateway_alert())
    assert rig.incident().status == "closed"

    # The slow-burn alert of the same episode (started before closure) is re-notified.
    rig.clock.advance(30)
    await rig.run(gateway_alert())
    await rig.run(gateway_alert(name="SLOBurnRateSlow", fingerprint="fp-slow"))
    assert len(rig.store.list_incidents()) == 2  # fp-slow was never part of the incident

    # Same fingerprint, but a new episode that started after the incident closed.
    later = gateway_alert()
    later["startsAt"] = (rig.clock.now + timedelta(seconds=60)).isoformat()
    await rig.run(later)
    assert len(rig.store.list_incidents()) == 3
    assert rig.executor.calls[0] == ("restart", "orders", None)


async def test_one_approval_covers_sibling_incidents_waiting_for_the_same_rollback(tmp_path: Path):
    rig = PipelineRig(tmp_path)
    rig.bad_deploy()
    await rig.page(gateway_alert())
    await rig.page(orders_alert())
    await rig.settle()
    assert [rig.incident(i).state for i in (0, 1)] == [State.PENDING_APPROVAL] * 2

    await rig.pipeline.approve(rig.incident(0).id, "alice")
    await rig.settle()

    assert [rig.incident(i).state for i in (0, 1)] == [State.RESOLVED, State.RESOLVED]
    assert rig.executor.calls == [("rollback", "orders", "v1")]
    assert rig.store.pending_actions() == []
    sibling = rig.incident(1)
    assert rig.store.actions(sibling.id)[0]["status"] == "covered"
    assert "ACTING" not in rig.states(sibling.id)
    covered = [e for e in rig.store.audit_entries(sibling.id) if e["action"] == "action_covered"]
    assert covered[0]["data"]["covered_by"] == rig.incident(0).id


async def test_incident_arriving_during_an_in_flight_remediation_waits_instead_of_diagnosing(
    tmp_path: Path,
):
    """Regression: seen live. A sibling diagnosed mid-scale-out proposed a stale replica count."""
    rig = PipelineRig(tmp_path)
    rig.saturation()
    await rig.page(gateway_alert())
    first = rig.incident(0)
    for state in (
        State.ENRICHED,
        State.DIAGNOSED,
        State.VALIDATED,
        State.POLICY_DECIDED,
        State.ACTING,
    ):
        first.state = state
    first.category = Category.LOAD_SATURATION
    first.root_cause_service = "orders"
    rig.store.save_incident(first)
    rig.store.add_action(
        first.id,
        action_type="scale_service",
        target="orders",
        params={"replicas": 2},
        risk="low",
        decision="allow",
        rule="low_risk_auto",
        dry_run=False,
        status="executing",
        ts=rig.clock.now,
    )

    sibling_pipeline = rig.new_pipeline()
    rig.pipeline = sibling_pipeline
    await rig.page(orders_alert())
    await sibling_pipeline.start()
    await sibling_pipeline.drain()
    await sibling_pipeline.stop()

    sibling = rig.incident(1)
    assert sibling.state is State.RESOLVED
    assert rig.states(sibling.id) == ["RECEIVED", "VERIFYING", "RESOLVED"]
    assert rig.store.diagnoses(sibling.id) == []
    assert (sibling.category, sibling.root_cause_service) == (Category.LOAD_SATURATION, "orders")
    covered = rig.store.actions(sibling.id)[0]
    assert (covered["status"], covered["rule"]) == ("covered", "remediation_in_flight")
    assert rig.executor.calls == []


async def test_covered_incident_is_diagnosed_afresh_if_the_other_remediation_does_not_help(
    tmp_path: Path,
):
    rig = PipelineRig(tmp_path)
    rig.error_spike()
    await rig.page(gateway_alert())
    first = rig.incident(0)
    first.state = State.ACTING
    rig.store.save_incident(first)
    other_action = rig.store.add_action(
        first.id,
        action_type="scale_service",
        target="orders",
        params={"replicas": 2},
        risk="low",
        decision="allow",
        rule="low_risk_auto",
        dry_run=False,
        status="executing",
        ts=rig.clock.now,
    )
    rig.recovered(False)

    async def restart_fixes_it(service: str) -> ActionResult:
        rig.executor.calls.append(("restart", service, None))
        rig.recovered(True)
        return ActionResult(success=True, detail="restarted")

    rig.executor.restart = restart_fixes_it  # type: ignore[method-assign]
    original_verify = rig.verifier.verify

    async def other_remediation_ends_without_effect(*args, **kwargs):
        rig.store.update_action(other_action, status="failed")
        return await original_verify(*args, **kwargs)

    rig.verifier.verify = other_remediation_ends_without_effect  # type: ignore[method-assign]
    sibling_pipeline = rig.new_pipeline()
    rig.pipeline = sibling_pipeline
    await rig.page(orders_alert())
    await sibling_pipeline.start()
    await sibling_pipeline.drain()
    await sibling_pipeline.stop()

    sibling = rig.incident(1)
    assert rig.states(sibling.id)[:3] == ["RECEIVED", "VERIFYING", "FAILED_VERIFICATION"]
    assert sibling.state is State.RESOLVED
    assert sibling.replans == 1
    assert rig.executor.calls == [("restart", "orders", None)]
    assert len(rig.store.diagnoses(sibling.id)) == 1
