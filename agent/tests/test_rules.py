from datetime import timedelta

import pytest

from agent.app.domain import ActionType, Category
from agent.diagnosis.rules import diagnose, localize
from agent.tests.fakes import EvidenceRig

A, C = ActionType, Category


async def run(setup, service: str = "gateway", slo: str | None = "gateway-availability"):
    rig = EvidenceRig()
    setup(rig)
    bundle = await rig.bundle(service, slo)
    return diagnose(bundle, rig.settings), bundle


# The ground-truth table of the fault taxonomy: scenario -> category, root cause, action.
TAXONOMY = [
    ("error_spike", lambda r: r.error_spike(), C.ERROR_SPIKE, "orders", A.RESTART, {}),
    ("saturation", lambda r: r.saturation(), C.LOAD_SATURATION, "orders", A.SCALE, {"replicas": 2}),
    (
        "crash_loop_after_deploy",
        lambda r: r.crash_loop(),
        C.CRASH_LOOP,
        "orders",
        A.ROLLBACK,
        {"to_version": "v1"},
    ),
    (
        "crash_loop_without_deploy",
        lambda r: r.crash_loop(deployed=False),
        C.CRASH_LOOP,
        "orders",
        A.RESTART,
        {},
    ),
    ("memory_leak", lambda r: r.memory_leak(), C.MEMORY_LEAK, "gateway", A.RESTART, {}),
    (
        "bad_deploy",
        lambda r: r.bad_deploy(),
        C.BAD_DEPLOY,
        "orders",
        A.ROLLBACK,
        {"to_version": "v1"},
    ),
    ("healthy", lambda r: None, C.UNKNOWN, "gateway", A.ESCALATE, {}),
]


@pytest.mark.parametrize(
    ("setup", "category", "root_cause", "action", "params"),
    [case[1:] for case in TAXONOMY],
    ids=[case[0] for case in TAXONOMY],
)
async def test_taxonomy(setup, category, root_cause, action, params):
    diagnosis, bundle = await run(setup)
    assert diagnosis.category is category
    assert diagnosis.root_cause_service == root_cause
    assert diagnosis.action.type is action
    assert diagnosis.action.target_service == root_cause
    assert diagnosis.action.params == params
    # Every citation must point at evidence that really is in the bundle.
    assert {c.id for c in diagnosis.evidence} <= bundle.ids()
    if category is C.UNKNOWN:
        assert diagnosis.insufficient_evidence is True
        assert diagnosis.confidence < 0.6
    else:
        assert diagnosis.evidence
        assert diagnosis.confidence >= 0.6
        assert len(diagnosis.rationale) <= 600


async def test_symptom_at_gateway_is_localised_to_orders():
    rig = EvidenceRig()
    rig.error_spike("orders")
    bundle = await rig.bundle("gateway")
    assert localize(bundle, rig.settings) == "orders"


async def test_gateway_only_fault_stays_at_the_gateway():
    rig = EvidenceRig()
    rig.error_spike("gateway")
    bundle = await rig.bundle("gateway")
    assert localize(bundle, rig.settings) == "gateway"
    diagnosis = diagnose(bundle, rig.settings)
    assert (diagnosis.category, diagnosis.root_cause_service) == (C.ERROR_SPIKE, "gateway")


async def test_crash_loop_wins_over_everything_else():
    def setup(rig: EvidenceRig) -> None:
        rig.saturation()
        rig.memory_leak("orders")
        rig.crash_loop()

    diagnosis, _ = await run(setup)
    assert diagnosis.category is C.CRASH_LOOP


async def test_crash_loop_detected_from_the_runtime_when_metrics_are_gone():
    def setup(rig: EvidenceRig) -> None:
        rig.crash_loop()
        rig.metrics.set("restarts_5m", "orders", None)

    diagnosis, _ = await run(setup)
    assert diagnosis.category is C.CRASH_LOOP
    assert "container_restarts@orders" in {c.id for c in diagnosis.evidence}


async def test_deploy_older_than_ten_minutes_is_an_error_spike_not_a_bad_deploy():
    def setup(rig: EvidenceRig) -> None:
        rig.error_spike()
        rig.deploys.add("orders", "v2-bad", "v1", rig.clock.now - timedelta(seconds=900))

    diagnosis, _ = await run(setup)
    assert diagnosis.category is C.ERROR_SPIKE
    assert diagnosis.action.type is A.RESTART


async def test_recent_deploy_with_healthy_metrics_is_not_a_bad_deploy():
    def setup(rig: EvidenceRig) -> None:
        rig.deploys.add("orders", "v2", "v1", rig.clock.now - timedelta(seconds=60))

    diagnosis, _ = await run(setup)
    assert diagnosis.category is C.UNKNOWN


async def test_bad_deploy_wins_over_error_spike_and_memory():
    def setup(rig: EvidenceRig) -> None:
        rig.bad_deploy()
        rig.memory_leak("orders")

    diagnosis, _ = await run(setup)
    assert diagnosis.category is C.BAD_DEPLOY


async def test_high_memory_without_growth_is_not_a_leak():
    def setup(rig: EvidenceRig) -> None:
        rig.metrics.set_many("gateway", {"rss_pct_of_limit": 0.8, "rss_slope_1m": 0.0})

    diagnosis, _ = await run(setup)
    assert diagnosis.category is C.UNKNOWN


async def test_growth_below_seventy_percent_is_not_a_leak_unless_predicted():
    def growing(rig: EvidenceRig) -> None:
        rig.metrics.set_many("gateway", {"rss_pct_of_limit": 0.5, "rss_slope_1m": 2e6})

    assert (await run(growing))[0].category is C.UNKNOWN

    def predicted(rig: EvidenceRig) -> None:
        growing(rig)
        rig.metrics.set("alert_firing", "gateway", {"MemoryExhaustionPredicted": 1.0})

    assert (await run(predicted))[0].category is C.MEMORY_LEAK


async def test_saturation_needs_both_busy_workers_and_a_load_surge():
    def busy_at_normal_load(rig: EvidenceRig) -> None:
        rig.saturation()
        rig.metrics.set_many("orders", {"request_rate_1m": 22.0, "request_rate_baseline": 20.0})

    diagnosis, _ = await run(busy_at_normal_load)
    assert diagnosis.category is C.ERROR_SPIKE

    def surge_without_saturation(rig: EvidenceRig) -> None:
        rig.metrics.set_many("orders", {"request_rate_1m": 150.0, "worker_utilization": 0.6})

    assert (await run(surge_without_saturation))[0].category is C.UNKNOWN


async def test_saturation_without_a_baseline_cannot_be_called_a_surge():
    def setup(rig: EvidenceRig) -> None:
        rig.saturation()
        rig.metrics.set("request_rate_baseline", "orders", None)

    assert (await run(setup))[0].category is C.ERROR_SPIKE


async def test_scale_target_follows_the_running_replica_count():
    def setup(rig: EvidenceRig) -> None:
        rig.saturation()
        rig.executor.containers["orders"] = [
            *rig.executor.containers["orders"],
            rig.executor.containers["orders"][0].model_copy(update={"name": "relialoop-orders-2"}),
        ]

    diagnosis, _ = await run(setup)
    assert diagnosis.action.params == {"replicas": 3}


async def test_latency_alone_escalates():
    def setup(rig: EvidenceRig) -> None:
        rig.metrics.set_many("orders", {"p99_latency_1m": 2.0})

    diagnosis, _ = await run(setup)
    assert diagnosis.category is C.UNKNOWN
    assert diagnosis.root_cause_service == "orders"
    assert diagnosis.action.type is A.ESCALATE


async def test_missing_metrics_escalate_instead_of_guessing():
    def setup(rig: EvidenceRig) -> None:
        rig.metrics.fail = True

    diagnosis, _ = await run(setup)
    assert diagnosis.category is C.UNKNOWN
    assert diagnosis.insufficient_evidence is True
    assert "Metrics were unavailable" in diagnosis.rationale


async def test_error_ratio_just_below_threshold_is_not_a_spike():
    assert (await run(lambda r: r.error_spike(ratio=0.049)))[0].category is C.UNKNOWN
    assert (await run(lambda r: r.error_spike(ratio=0.05)))[0].category is C.ERROR_SPIKE


async def test_orders_incident_is_diagnosed_without_gateway_evidence():
    diagnosis, bundle = await run(lambda r: r.error_spike(), "orders", "orders-availability")
    assert bundle.services == ("orders",)
    assert (diagnosis.category, diagnosis.root_cause_service) == (C.ERROR_SPIKE, "orders")


async def test_diagnosis_is_deterministic():
    first, _ = await run(lambda r: r.bad_deploy())
    second, _ = await run(lambda r: r.bad_deploy())
    assert first == second
