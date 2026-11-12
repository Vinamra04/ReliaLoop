from datetime import timedelta
from pathlib import Path

import pytest

from agent.adapters.sqlite_store import SqliteStore
from agent.app.domain import ActionType, Decision, ProposedAction, State
from agent.policy.engine import PolicyEngine
from agent.policy.limits import (
    Limits,
    Policy,
    PolicyError,
    WindowLimit,
    load_policy,
    record_execution,
)
from agent.tests.fakes import T0, cmdb
from tools import paths

A = ActionType
RESTART = ProposedAction(type=A.RESTART, target_service="orders")
SCALE = ProposedAction(type=A.SCALE, target_service="orders", params={"replicas": 2})
ROLLBACK = ProposedAction(type=A.ROLLBACK, target_service="orders", params={"to_version": "v1"})
POLICY_FILE = paths.REPO_ROOT / "agent" / "policy.yaml"


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    db = SqliteStore(tmp_path / "agent.db")
    db.migrate()
    return db


@pytest.fixture
def engine(store: SqliteStore) -> PolicyEngine:
    return PolicyEngine(load_policy(POLICY_FILE), cmdb(), store)


def incident(store: SqliteStore, service: str = "gateway"):
    return store.create_incident(
        service=service,
        slo=f"{service}-availability",
        severity="page",
        mode="enforce",
        diagnosis_mode="rules",
        summary="s",
        now=T0,
    )


def execute(store: SqliteStore, incident_id: str, action: ProposedAction, at, *, state=None):
    """Record an executed action the way the pipeline does."""
    action_id = store.add_action(
        incident_id,
        action_type=action.type.value,
        target=action.target_service,
        params=action.params,
        risk="low",
        decision="allow",
        rule="low_risk_auto",
        dry_run=False,
        status="executed",
        ts=at,
    )
    store.update_action(action_id, started_at=at, finished_at=at)
    record_execution(
        store,
        action=action.type.value,
        target=action.target_service,
        incident_id=incident_id,
        now=at,
    )
    if state:
        current = store.get_incident(incident_id)
        current.state = state
        store.save_incident(current)
    return action_id


def decide(engine: PolicyEngine, action=RESTART, *, incident_id="INC-X", mode="enforce", at=T0):
    return engine.evaluate(action, incident_id=incident_id, mode=mode, now=at)


def test_repository_policy_matches_the_plan():
    policy = load_policy(POLICY_FILE)
    assert policy.risk == {
        "restart_service": "low",
        "scale_service": "low",
        "rollback_service": "high",
    }
    assert policy.approval.required_for == ("high",)
    assert policy.approval.timeout_s == 600
    assert policy.limits.restarts_per_service == WindowLimit(max=2, window_s=900)
    assert policy.limits.actions_per_incident == 2
    assert policy.risk_of("format_disk") == "high"


def test_invalid_policy_files_are_rejected(tmp_path: Path):
    with pytest.raises(PolicyError):
        load_policy(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("risk: {restart_service: medium}", encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policy(bad)


@pytest.mark.parametrize(
    ("action", "decision", "rule", "risk"),
    [
        (RESTART, Decision.ALLOW, "low_risk_auto", "low"),
        (SCALE, Decision.ALLOW, "low_risk_auto", "low"),
        (ROLLBACK, Decision.REQUIRE_APPROVAL, "high_risk_needs_approval", "high"),
    ],
)
def test_risk_classes_in_enforce_mode(engine, action, decision, rule, risk):
    result = decide(engine, action)
    assert (result.decision, result.rule, result.risk) == (decision, rule, risk)


@pytest.mark.parametrize("action", [RESTART, SCALE, ROLLBACK])
def test_dry_run_never_executes_or_asks_for_approval(engine, action):
    result = decide(engine, action, mode="dry_run")
    assert result.decision is Decision.DRY_RUN
    assert result.detail.startswith("would execute")


def test_observe_mode_never_executes(engine):
    result = decide(engine, mode="observe")
    assert (result.decision, result.rule) == (Decision.DENY, "mode_does_not_execute")


@pytest.mark.parametrize("target", ["postgres", "billing"])
def test_unmanaged_and_unknown_services_are_never_touched(engine, target):
    result = decide(engine, ProposedAction(type=A.RESTART, target_service=target))
    assert (result.decision, result.rule) == (Decision.DENY, "unmanaged_service")


def test_cooldown_blocks_a_second_action_by_the_same_incident(engine, store):
    first = incident(store)
    execute(store, first.id, RESTART, T0)
    blocked = decide(engine, SCALE, incident_id=first.id, at=T0 + timedelta(seconds=30))
    assert (blocked.decision, blocked.rule) == (Decision.DENY, "cooldown")
    allowed = decide(engine, SCALE, incident_id=first.id, at=T0 + timedelta(seconds=61))
    assert allowed.decision is Decision.ALLOW


def test_cooldown_is_per_service(engine, store):
    execute(store, incident(store).id, RESTART, T0)
    other = ProposedAction(type=A.RESTART, target_service="gateway")
    assert decide(engine, other, at=T0 + timedelta(seconds=5)).decision is Decision.ALLOW


def test_same_action_in_flight_for_another_incident_is_covered(engine, store):
    first = incident(store, "orders")
    execute(store, first.id, RESTART, T0, state=State.VERIFYING)
    second = incident(store, "gateway")
    result = decide(engine, RESTART, incident_id=second.id, at=T0 + timedelta(seconds=5))
    assert result.decision is Decision.COVERED
    assert result.covered_by == first.id
    assert result.rule == "same_action_in_flight"


def test_different_action_in_flight_is_denied(engine, store):
    first = incident(store, "orders")
    execute(store, first.id, RESTART, T0, state=State.VERIFYING)
    result = decide(engine, SCALE, incident_id=incident(store).id, at=T0 + timedelta(seconds=5))
    assert (result.decision, result.rule) == (Decision.DENY, "action_in_flight")


def test_same_action_just_finished_for_another_incident_is_covered(engine, store):
    first = incident(store, "orders")
    execute(store, first.id, RESTART, T0, state=State.RESOLVED)
    result = decide(engine, RESTART, incident_id=incident(store).id, at=T0 + timedelta(seconds=20))
    assert (result.decision, result.rule) == (Decision.COVERED, "same_action_in_cooldown")
    assert result.covered_by == first.id


def test_restart_limit_per_service(engine, store):
    for minutes in (0, 5):
        execute(store, incident(store).id, RESTART, T0 + timedelta(minutes=minutes))
    blocked = decide(engine, at=T0 + timedelta(minutes=10))
    assert (blocked.decision, blocked.rule) == (Decision.DENY, "restart_limit")
    # Other actions on the service are still possible, and the window slides.
    assert decide(engine, SCALE, at=T0 + timedelta(minutes=10)).decision is Decision.ALLOW
    assert decide(engine, at=T0 + timedelta(minutes=16)).decision is Decision.ALLOW


def test_actions_per_incident_limit(engine, store):
    current = incident(store)
    execute(store, current.id, RESTART, T0)
    execute(store, current.id, SCALE, T0 + timedelta(minutes=2))
    result = decide(engine, ROLLBACK, incident_id=current.id, at=T0 + timedelta(minutes=5))
    assert (result.decision, result.rule) == (Decision.DENY, "actions_per_incident")


def test_global_hourly_limit(store):
    policy = Policy(
        risk={"restart_service": "low"}, limits=Limits(actions_per_hour=2, cooldown_s=0)
    )
    engine = PolicyEngine(policy, cmdb(), store)
    gateway_restart = ProposedAction(type=A.RESTART, target_service="gateway")
    execute(store, incident(store).id, RESTART, T0)
    execute(store, incident(store).id, gateway_restart, T0 + timedelta(minutes=1))
    blocked = decide(engine, at=T0 + timedelta(minutes=2))
    assert (blocked.decision, blocked.rule) == (Decision.DENY, "actions_per_hour")
    assert decide(engine, at=T0 + timedelta(minutes=62)).decision is Decision.ALLOW


@pytest.mark.parametrize(
    ("replicas", "rule"), [(4, "max_replicas"), (0, "min_replicas"), ("2", "max_replicas")]
)
def test_replica_limits_from_the_cmdb(engine, replicas, rule):
    action = ProposedAction(type=A.SCALE, target_service="orders", params={"replicas": replicas})
    result = decide(engine, action)
    assert (result.decision, result.rule) == (Decision.DENY, rule)


def test_scale_to_the_ceiling_is_allowed(engine):
    action = ProposedAction(type=A.SCALE, target_service="orders", params={"replicas": 3})
    assert decide(engine, action).decision is Decision.ALLOW


def test_limits_survive_a_restart_of_the_agent(tmp_path: Path):
    path = tmp_path / "agent.db"
    first = SqliteStore(path)
    first.migrate()
    for minutes in (0, 5):
        execute(first, incident(first).id, RESTART, T0 + timedelta(minutes=minutes))
    first.close()

    reopened = SqliteStore(path)
    reopened.migrate()
    engine = PolicyEngine(load_policy(POLICY_FILE), cmdb(), reopened)
    assert decide(engine, at=T0 + timedelta(minutes=10)).rule == "restart_limit"


def test_limits_are_checked_before_dry_run(engine, store):
    """Dry-run reports what would really happen, including a denial."""
    execute(store, incident(store).id, RESTART, T0)
    current = incident(store)
    execute(store, current.id, RESTART, T0 + timedelta(minutes=2))
    result = decide(engine, mode="dry_run", incident_id=current.id, at=T0 + timedelta(minutes=5))
    assert (result.decision, result.rule) == (Decision.DENY, "restart_limit")
