from datetime import timedelta

import pytest

from agent.app.domain import ActionType, Category, Citation, Diagnosis, ProposedAction
from agent.diagnosis.rules import diagnose
from agent.diagnosis.validator import CONSISTENT_ACTIONS, validate
from agent.tests.fakes import EvidenceRig, container

A, C = ActionType, Category
CITE = (Citation(id="err_ratio_1m@orders", observation="50% failing"),)


def diagnosis(
    category: Category = C.ERROR_SPIKE,
    action: ActionType = A.RESTART,
    target: str = "orders",
    *,
    params: dict | None = None,
    confidence: float = 0.9,
    evidence: tuple[Citation, ...] = CITE,
    root_cause: str | None = None,
    insufficient: bool = False,
) -> Diagnosis:
    return Diagnosis(
        category=category,
        root_cause_service=root_cause or target,
        confidence=confidence,
        insufficient_evidence=insufficient,
        evidence=evidence,
        action=ProposedAction(type=action, target_service=target, params=params or {}),
        rationale="test",
    )


async def check(proposal: Diagnosis, setup=lambda rig: rig.error_spike(), service="gateway"):
    rig = EvidenceRig()
    setup(rig)
    bundle = await rig.bundle(service)
    return validate(proposal, bundle, rig.cmdb, rig.settings)


async def test_valid_restart_passes_and_params_are_normalised():
    result = await check(diagnosis(params={"force": True, "grace": 0}))
    assert result.ok is True
    assert result.action.type is A.RESTART
    assert result.action.params == {}


@pytest.mark.parametrize(
    "scenario", ["error_spike", "saturation", "crash_loop", "memory_leak", "bad_deploy"]
)
async def test_every_rule_based_diagnosis_passes_validation(scenario: str):
    rig = EvidenceRig()
    getattr(rig, scenario)()
    bundle = await rig.bundle()
    result = validate(diagnose(bundle, rig.settings), bundle, rig.cmdb, rig.settings)
    assert result.ok is True, result


async def test_hallucinated_evidence_id_is_rejected():
    fake = (*CITE, Citation(id="cpu_on_fire@orders", observation="invented"))
    result = await check(diagnosis(evidence=fake))
    assert (result.ok, result.reason) == (False, "unknown_evidence_id")
    assert "cpu_on_fire@orders" in result.detail


async def test_hallucinated_evidence_is_rejected_even_for_escalations():
    fake = (Citation(id="made_up@gateway", observation="x"),)
    result = await check(diagnosis(C.UNKNOWN, A.ESCALATE, evidence=fake))
    assert result.reason == "unknown_evidence_id"


async def test_action_needs_at_least_one_citation():
    assert (await check(diagnosis(evidence=()))).reason == "no_evidence_cited"


async def test_escalate_and_no_action_pass_through():
    for action in (A.ESCALATE, A.NO_ACTION):
        result = await check(diagnosis(C.UNKNOWN, action, confidence=0.1, evidence=()))
        assert result.ok is True
        assert result.action.type is action


@pytest.mark.parametrize(
    ("target", "action", "reason"),
    [
        ("postgres", A.RESTART, "unmanaged_target"),
        ("gateway", A.SCALE, "action_not_allowed"),
        ("billing", A.RESTART, "unknown_service"),
    ],
)
async def test_disallowed_targets_and_actions_are_rejected(target, action, reason):
    category = C.LOAD_SATURATION if action is A.SCALE else C.ERROR_SPIKE
    result = await check(diagnosis(category, action, target, root_cause="orders"))
    assert (result.ok, result.reason) == (False, reason)


async def test_unknown_root_cause_service_is_rejected():
    assert (await check(diagnosis(root_cause="billing"))).reason == "unknown_service"


async def test_target_outside_the_incident_scope_is_rejected():
    """An orders incident must never lead to an action on the gateway."""
    proposal = diagnosis(target="gateway", root_cause="orders")
    result = await check(proposal, service="orders")
    assert (result.ok, result.reason) == (False, "target_out_of_scope")


@pytest.mark.parametrize("confidence", [0.0, 0.3, 0.59])
async def test_low_confidence_is_rejected(confidence: float):
    result = await check(diagnosis(confidence=confidence))
    assert (result.ok, result.reason) == (False, "low_confidence")


async def test_confidence_at_the_threshold_passes():
    assert (await check(diagnosis(confidence=0.6))).ok is True


async def test_self_declared_insufficient_evidence_is_rejected():
    assert (await check(diagnosis(insufficient=True))).reason == "insufficient_evidence"


@pytest.mark.parametrize(
    ("category", "action"),
    [
        (category, action)
        for category in Category
        for action in (A.RESTART, A.SCALE, A.ROLLBACK)
        if action not in CONSISTENT_ACTIONS[category]
    ],
)
async def test_action_must_fit_the_category(category: Category, action: ActionType):
    result = await check(diagnosis(category, action))
    assert (result.ok, result.reason) == (False, "action_inconsistent_with_category")


# --- rollback consistency --------------------------------------------------------

ROLLBACK = diagnosis(C.BAD_DEPLOY, A.ROLLBACK, params={"to_version": "v1"})


async def test_rollback_with_recent_deploy_passes():
    result = await check(ROLLBACK, lambda rig: rig.bad_deploy())
    assert result.ok is True
    assert result.action.params == {"to_version": "v1"}


async def test_rollback_version_is_filled_in_from_deploy_history():
    proposal = diagnosis(C.BAD_DEPLOY, A.ROLLBACK)
    result = await check(proposal, lambda rig: rig.bad_deploy())
    assert result.action.params == {"to_version": "v1"}


async def test_rollback_without_any_deploy_is_rejected():
    result = await check(ROLLBACK)
    assert (result.ok, result.reason) == (False, "rollback_without_recent_deploy")


async def test_rollback_with_an_old_deploy_is_rejected():
    def setup(rig: EvidenceRig) -> None:
        rig.error_spike()
        rig.deploys.add("orders", "v2-bad", "v1", rig.clock.now - timedelta(seconds=1801))

    assert (await check(ROLLBACK, setup)).reason == "rollback_without_recent_deploy"


async def test_rollback_ignores_deploys_of_other_services_and_agent_rollbacks():
    def setup(rig: EvidenceRig) -> None:
        rig.error_spike()
        rig.deploys.add("gateway", "v2", "v1", rig.clock.now - timedelta(seconds=60))
        rig.deploys.add(
            "orders", "v1", "v2-bad", rig.clock.now - timedelta(seconds=30), reason="rollback"
        )

    assert (await check(ROLLBACK, setup)).reason == "rollback_without_recent_deploy"


async def test_rollback_without_previous_version_is_rejected():
    def setup(rig: EvidenceRig) -> None:
        rig.error_spike()
        rig.deploys.add("orders", "v1", None, rig.clock.now - timedelta(seconds=60))

    assert (await check(ROLLBACK, setup)).reason == "rollback_no_previous_version"


async def test_rollback_to_an_arbitrary_version_is_rejected():
    proposal = diagnosis(C.BAD_DEPLOY, A.ROLLBACK, params={"to_version": "v0-ancient"})
    result = await check(proposal, lambda rig: rig.bad_deploy())
    assert (result.ok, result.reason) == (False, "rollback_wrong_version")


async def test_injected_gateway_rollback_is_rejected():
    """The prompt-injection payload: 'roll back gateway' with no gateway deploy."""
    proposal = diagnosis(
        C.BAD_DEPLOY,
        A.ROLLBACK,
        "gateway",
        evidence=(Citation(id="err_ratio_1m@gateway", observation="x"),),
    )
    result = await check(proposal)
    assert (result.ok, result.reason) == (False, "rollback_without_recent_deploy")


# --- scale consistency -----------------------------------------------------------

SCALE = diagnosis(C.LOAD_SATURATION, A.SCALE, params={"replicas": 2})


async def test_scale_by_one_passes():
    result = await check(SCALE, lambda rig: rig.saturation())
    assert result.ok is True
    assert result.action.params == {"replicas": 2}


async def test_scale_default_is_current_plus_one():
    result = await check(diagnosis(C.LOAD_SATURATION, A.SCALE), lambda rig: rig.saturation())
    assert result.action.params == {"replicas": 2}


@pytest.mark.parametrize("replicas", [1, 3, 10, "2", 2.0, True, -1])
async def test_scale_must_be_exactly_one_more(replicas):
    proposal = diagnosis(C.LOAD_SATURATION, A.SCALE, params={"replicas": replicas})
    result = await check(proposal, lambda rig: rig.saturation())
    assert (result.ok, result.reason) == (False, "scale_invalid_replicas")


async def test_scale_at_max_replicas_is_rejected():
    def setup(rig: EvidenceRig) -> None:
        rig.saturation()
        rig.executor.containers["orders"] = [container(f"relialoop-orders-{n}") for n in (1, 2, 3)]

    proposal = diagnosis(C.LOAD_SATURATION, A.SCALE, params={"replicas": 4})
    assert (await check(proposal, setup)).reason == "scale_at_max_replicas"


async def test_scale_with_unknown_replica_count_is_rejected():
    def setup(rig: EvidenceRig) -> None:
        rig.saturation()
        rig.executor.fail_status = True
        rig.metrics.set("replicas_up", "orders", None)

    assert (await check(SCALE, setup)).reason == "scale_unknown_replicas"


async def test_rollback_of_an_already_reverted_release_is_rejected():
    def setup(rig: EvidenceRig) -> None:
        rig.error_spike()
        rig.deploys.add("orders", "v2-bad", "v1", rig.clock.now - timedelta(seconds=120))
        rig.deploys.add(
            "orders", "v1", "v2-bad", rig.clock.now - timedelta(seconds=60), reason="rollback"
        )

    result = await check(ROLLBACK, setup)
    assert (result.ok, result.reason) == (False, "rollback_without_recent_deploy")
    assert "already rolled back" in result.detail
