from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent.adapters.sqlite_store import GENESIS_HASH, SqliteStore
from agent.app.domain import (
    ActionType,
    Category,
    DeployEvent,
    Diagnosis,
    DiagnosisRecord,
    EvidenceBundle,
    EvidenceItem,
    ProposedAction,
    State,
)
from agent.app.models import AmAlert
from agent.tests.fixtures import alert

NOW = datetime(2026, 11, 12, 8, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    db = SqliteStore(tmp_path / "agent.db")
    db.migrate()
    return db


def new_incident(store: SqliteStore, service="gateway", slo="gateway-availability", now=NOW):
    return store.create_incident(
        service=service,
        slo=slo,
        severity="page",
        mode="enforce",
        diagnosis_mode="rules",
        summary="burning",
        now=now,
    )


def test_migrations_apply_once_and_set_the_version(tmp_path: Path):
    path = tmp_path / "agent.db"
    first = SqliteStore(path)
    assert first.migrate() == [1]
    assert first.migrate() == []
    assert first.schema_version() == 1
    first.close()
    reopened = SqliteStore(path)
    assert reopened.migrate() == []
    reopened.ping()


def test_incident_ids_are_sequential_per_day(store: SqliteStore):
    first = new_incident(store)
    second = new_incident(store, service="orders", slo="orders-availability")
    next_day = new_incident(store, now=NOW + timedelta(days=1))
    assert [first.id, second.id, next_day.id] == [
        "INC-20261112-001",
        "INC-20261112-002",
        "INC-20261113-001",
    ]


def test_incident_round_trip_and_open_lookup(store: SqliteStore):
    incident = new_incident(store)
    incident.state = State.DIAGNOSED
    incident.category = Category.ERROR_SPIKE
    incident.root_cause_service = "orders"
    incident.degraded_mode = True
    store.save_incident(incident)

    loaded = store.get_incident(incident.id)
    assert loaded == incident
    assert store.find_open_incident("gateway", "gateway-availability").id == incident.id
    assert store.find_open_incident("gateway", None) is None
    assert store.get_incident("INC-missing") is None

    incident.status = "closed"
    incident.closed_at = NOW
    store.save_incident(incident)
    assert store.find_open_incident("gateway", "gateway-availability") is None
    assert [i.id for i in store.list_incidents(status="closed")] == [incident.id]
    assert store.list_incidents(status="open") == []


def test_open_lookup_treats_missing_slo_as_its_own_key(store: SqliteStore):
    without_slo = new_incident(store, service="gateway", slo=None)
    assert store.find_open_incident("gateway", None).id == without_slo.id
    assert store.find_open_incident("gateway", "gateway-availability") is None


def test_timeline_events_are_ordered_with_data(store: SqliteStore):
    incident = new_incident(store)
    store.add_event(incident.id, state=State.RECEIVED, kind="state", message="a", data=None, ts=NOW)
    store.add_event(
        incident.id, state=State.ENRICHED, kind="state", message="b", data={"items": 3}, ts=NOW
    )
    events = store.events(incident.id)
    assert [(e["state"], e["message"], e["data"]) for e in events] == [
        ("RECEIVED", "a", {}),
        ("ENRICHED", "b", {"items": 3}),
    ]


def test_alert_upsert_is_idempotent_and_tracks_resolution(store: SqliteStore):
    incident = new_incident(store)
    firing = AmAlert.model_validate(alert())
    assert store.upsert_alert(incident.id, firing, NOW) == "new"
    assert store.upsert_alert(incident.id, firing, NOW) == "duplicate"
    assert store.firing_alerts(incident.id) == 1
    assert store.open_incident_for_fingerprint(firing.fingerprint).id == incident.id

    resolved = AmAlert.model_validate(alert(status="resolved"))
    assert store.upsert_alert(incident.id, resolved, NOW) == "changed"
    assert store.firing_alerts(incident.id) == 0
    stored = store.alerts(incident.id)[0]
    assert stored["labels"]["slo"] == "gateway-availability"
    assert stored["ends_at"] is not None
    assert store.open_incident_for_fingerprint("unknown") is None


def test_evidence_and_diagnosis_round_trip(store: SqliteStore):
    incident = new_incident(store)
    bundle = EvidenceBundle(
        incident_id=incident.id,
        alerting_service="gateway",
        slo="gateway-availability",
        services=("gateway", "orders"),
        collected_at=NOW,
        items=(
            EvidenceItem(
                id="err_ratio_1m@orders",
                service="orders",
                name="err_ratio_1m",
                window="1m",
                value=0.5,
                unit="ratio",
                observed_at=NOW,
            ),
        ),
    )
    store.add_evidence(bundle, 1)
    assert store.evidence(incident.id)[0]["bundle"]["items"][0]["id"] == "err_ratio_1m@orders"

    record = DiagnosisRecord(
        diagnosis=Diagnosis(
            category=Category.ERROR_SPIKE,
            root_cause_service="orders",
            confidence=0.9,
            action=ProposedAction(type=ActionType.RESTART, target_service="orders"),
            rationale="errors without deploy",
        ),
        mode="rules",
    )
    store.add_diagnosis(incident.id, record, NOW)
    stored = store.diagnoses(incident.id)[0]
    assert stored["mode"] == "rules"
    assert stored["diagnosis"]["action"]["type"] == "restart_service"


def test_action_lifecycle_and_in_flight_lookup(store: SqliteStore):
    incident = new_incident(store)
    action_id = store.add_action(
        incident.id,
        action_type="restart_service",
        target="orders",
        params={},
        risk="low",
        decision="allow",
        rule="low_risk_auto",
        dry_run=False,
        status="executing",
        ts=NOW,
    )
    assert store.in_flight_action("orders")["id"] == action_id
    assert store.in_flight_action("gateway") is None

    store.update_action(
        action_id, status="executed", finished_at=NOW, outcome={"success": True, "detail": "ok"}
    )
    assert store.in_flight_action("orders") is None
    incident.state = State.VERIFYING
    store.save_incident(incident)
    assert store.in_flight_action("orders")["id"] == action_id
    assert store.last_executed_action("orders")["outcome"] == {"success": True, "detail": "ok"}
    assert store.actions(incident.id)[0]["status"] == "executed"
    with pytest.raises(ValueError, match="cannot update"):
        store.update_action(action_id, incident_id="other")


def test_pending_actions(store: SqliteStore):
    incident = new_incident(store)
    store.add_action(
        incident.id,
        action_type="rollback_service",
        target="orders",
        params={"to_version": "v1"},
        risk="high",
        decision="approval_required",
        rule="high_risk_needs_approval",
        dry_run=False,
        status="pending_approval",
        ts=NOW,
    )
    pending = store.pending_actions()
    assert len(pending) == 1
    assert pending[0]["params"] == {"to_version": "v1"}


def test_limit_events_are_counted_in_a_window(store: SqliteStore):
    for minutes in (0, 5, 20):
        store.add_limit_event("restart", "orders", NOW - timedelta(minutes=minutes))
    assert store.count_limit_events("restart", "orders", NOW - timedelta(minutes=15)) == 2
    assert store.count_limit_events("restart", "gateway", NOW - timedelta(minutes=15)) == 0
    assert store.last_limit_event("restart", "orders") == NOW
    assert store.last_limit_event("restart", "gateway") is None


def test_deploy_history_is_newest_first(store: SqliteStore):
    for index, version in enumerate(["v1", "v2-bad"]):
        store.add_deploy(
            DeployEvent(
                service="orders",
                version=version,
                previous_version="v1" if index else None,
                status="completed",
                ts=NOW + timedelta(minutes=index),
            )
        )
    history = store.deploys("orders")
    assert [d.version for d in history] == ["v2-bad", "v1"]
    assert history[0].previous_version == "v1"
    assert store.deploys("gateway") == []
    assert len(store.deploys()) == 2


def test_tickets_upsert_by_key(store: SqliteStore):
    fields = {
        "key": "alert:abc",
        "severity": "ticket",
        "priority": "normal",
        "service": "orders",
        "slo": None,
        "summary": "crash loop",
        "runbook": "crash-loop",
        "incident_id": None,
        "links": {"dashboard": "http://x"},
        "now": NOW,
    }
    assert store.upsert_ticket(status="open", **fields) is True
    assert store.upsert_ticket(status="resolved", **fields) is False
    tickets = store.tickets()
    assert len(tickets) == 1
    assert tickets[0]["status"] == "resolved"
    assert tickets[0]["links"] == {"dashboard": "http://x"}
    store.set_ticket_status("alert:abc", "open", NOW)
    assert store.tickets()[0]["status"] == "open"


def test_audit_chain_is_intact_and_detects_tampering(store: SqliteStore):
    assert store.verify_audit_chain() == (True, 0)
    for index in range(3):
        store.audit(
            actor="agent", action="policy_decision", subject="INC-1", data={"n": index}, ts=NOW
        )
    entries = store.audit_entries()
    assert entries[0]["prev_hash"] == GENESIS_HASH
    assert entries[1]["prev_hash"] == entries[0]["hash"]
    assert store.verify_audit_chain() == (True, 3)
    assert len(store.audit_entries(subject="INC-1")) == 3
    assert store.audit_entries(subject="other") == []

    store._conn.execute("UPDATE audit_log SET data='{\"n\": 99}' WHERE id=2")
    assert store.verify_audit_chain() == (False, 3)


def test_deleting_an_audit_row_breaks_the_chain(store: SqliteStore):
    for index in range(3):
        store.audit(actor="agent", action="x", subject="s", data={"n": index}, ts=NOW)
    store._conn.execute("DELETE FROM audit_log WHERE id=2")
    assert store.verify_audit_chain()[0] is False


def test_runtime_state(store: SqliteStore):
    assert store.get_state("mode") is None
    store.set_state("mode", "enforce")
    store.set_state("mode", "observe")
    assert store.get_state("mode") == "observe"
