"""SQLite implementation of the incident store.

All SQL lives here, behind plain methods, so swapping to Postgres means
writing one more class with the same methods. The methods are synchronous;
the pipeline runs them in a thread pool (`asyncio.to_thread`). A single
connection guarded by a lock is enough for one agent process.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.app.domain import (
    Category,
    DeployEvent,
    DiagnosisRecord,
    EvidenceBundle,
    Incident,
    State,
)
from agent.app.models import AmAlert

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
GENESIS_HASH = "0" * 64
OPEN = "open"
CLOSED = "closed"


def _iso(ts: datetime) -> str:
    return ts.isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def audit_hash(prev_hash: str, ts: str, actor: str, action: str, subject: str, data: str) -> str:
    """sha256 over the previous hash and this row's content."""
    payload = "\n".join((prev_hash, ts, actor, action, subject, data))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _incident(row: sqlite3.Row) -> Incident:
    return Incident(
        id=row["id"],
        service=row["service"],
        slo=row["slo"],
        severity=row["severity"],
        state=State(row["state"]),
        status=row["status"],
        mode=row["mode"],
        diagnosis_mode=row["diagnosis_mode"],
        category=Category(row["category"]) if row["category"] else None,
        root_cause_service=row["root_cause_service"],
        degraded_mode=bool(row["degraded_mode"]),
        replans=row["replans"],
        summary=row["summary"],
        resolution=row["resolution"],
        escalation_reason=row["escalation_reason"],
        opened_at=datetime.fromisoformat(row["opened_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
    )


def _deploy(row: sqlite3.Row) -> DeployEvent:
    return DeployEvent(
        service=row["service"],
        version=row["version"],
        previous_version=row["previous_version"],
        status=row["status"],
        reason=row["reason"],
        incident_id=row["incident_id"],
        override_reason=row["override_reason"],
        actor=row["actor"],
        ts=datetime.fromisoformat(row["ts"]),
    )


def _with_json(row: sqlite3.Row, *columns: str) -> dict[str, Any]:
    out = dict(row)
    for column in columns:
        out[column] = json.loads(out[column])
    return out


class SqliteStore:
    """Incidents, timeline, alerts, evidence, diagnoses, actions, deploys, tickets, audit."""

    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- migrations -----------------------------------------------------------

    def migrate(self, directory: Path = MIGRATIONS_DIR) -> list[int]:
        """Apply pending `NNNN_name.sql` files in order; return the versions applied."""
        applied: list[int] = []
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version "
                "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            done = {
                row["version"] for row in self._conn.execute("SELECT version FROM schema_version")
            }
            for path in sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql")):
                version = int(path.name[:4])
                if version in done:
                    continue
                self._conn.execute("BEGIN")
                try:
                    for statement in _statements(path.read_text(encoding="utf-8")):
                        self._conn.execute(statement)
                    self._conn.execute(
                        "INSERT INTO schema_version VALUES (?, ?, datetime('now'))",
                        (version, path.name),
                    )
                    self._conn.execute("COMMIT")
                except sqlite3.Error:
                    self._conn.execute("ROLLBACK")
                    raise
                applied.append(version)
        return applied

    def schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT max(version) AS v FROM schema_version").fetchone()
        return int(row["v"] or 0)

    def ping(self) -> None:
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()

    # --- incidents ------------------------------------------------------------

    def create_incident(
        self,
        *,
        service: str,
        slo: str | None,
        severity: str,
        mode: str,
        diagnosis_mode: str,
        summary: str,
        now: datetime,
    ) -> Incident:
        """Create an incident with the next `INC-YYYYMMDD-NNN` id."""
        day = now.strftime("%Y%m%d")
        with self._lock:
            row = self._conn.execute(
                "SELECT count(*) AS n FROM incidents WHERE id LIKE ?", (f"INC-{day}-%",)
            ).fetchone()
            incident = Incident(
                id=f"INC-{day}-{row['n'] + 1:03d}",
                service=service,
                slo=slo,
                severity=severity,
                mode=mode,
                diagnosis_mode=diagnosis_mode,
                summary=summary,
                opened_at=now,
                updated_at=now,
            )
            self._conn.execute(
                "INSERT INTO incidents (id, service, slo, severity, state, status, mode, "
                "diagnosis_mode, summary, opened_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    incident.id,
                    service,
                    slo,
                    severity,
                    incident.state.value,
                    incident.status,
                    mode,
                    diagnosis_mode,
                    summary,
                    _iso(now),
                    _iso(now),
                ),
            )
        return incident

    def save_incident(self, incident: Incident) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE incidents SET state=?, status=?, mode=?, diagnosis_mode=?, category=?, "
                "root_cause_service=?, degraded_mode=?, replans=?, summary=?, resolution=?, "
                "escalation_reason=?, updated_at=?, closed_at=? WHERE id=?",
                (
                    incident.state.value,
                    incident.status,
                    incident.mode,
                    incident.diagnosis_mode,
                    incident.category.value if incident.category else None,
                    incident.root_cause_service,
                    int(incident.degraded_mode),
                    incident.replans,
                    incident.summary,
                    incident.resolution,
                    incident.escalation_reason,
                    _iso(incident.updated_at),
                    _iso(incident.closed_at) if incident.closed_at else None,
                    incident.id,
                ),
            )

    def get_incident(self, incident_id: str) -> Incident | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM incidents WHERE id=?", (incident_id,)
            ).fetchone()
        return _incident(row) if row else None

    def list_incidents(self, limit: int = 50, status: str | None = None) -> list[Incident]:
        query = "SELECT * FROM incidents"
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY opened_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_incident(row) for row in rows]

    def find_open_incident(self, service: str, slo: str | None) -> Incident | None:
        """The open incident for `(service, slo)`, if any (at most one exists)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM incidents WHERE status=? AND service=? AND ifnull(slo,'')=? "
                "ORDER BY opened_at DESC LIMIT 1",
                (OPEN, service, slo or ""),
            ).fetchone()
        return _incident(row) if row else None

    # --- timeline -------------------------------------------------------------

    def add_event(
        self,
        incident_id: str,
        *,
        state: State,
        kind: str,
        message: str,
        data: dict[str, Any] | None,
        ts: datetime,
    ) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO incident_events (incident_id, ts, state, kind, message, data) "
                "VALUES (?,?,?,?,?,?)",
                (incident_id, _iso(ts), state.value, kind, message, _json(data or {})),
            )
        return int(cursor.lastrowid or 0)

    def events(self, incident_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM incident_events WHERE incident_id=? ORDER BY id", (incident_id,)
            ).fetchall()
        return [_with_json(row, "data") for row in rows]

    # --- alerts ---------------------------------------------------------------

    def upsert_alert(self, incident_id: str, alert: AmAlert, received_at: datetime) -> str:
        """Store an alert for an incident. Returns `new`, `changed` or `duplicate`."""
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM alerts WHERE fingerprint=? AND incident_id=?",
                (alert.fingerprint, incident_id),
            ).fetchone()
            ends_at = _iso(alert.ends_at) if alert.ends_at and alert.status == "resolved" else None
            if row is None:
                self._conn.execute(
                    "INSERT INTO alerts VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        alert.fingerprint,
                        incident_id,
                        alert.name,
                        alert.status,
                        _json(alert.labels),
                        _json(alert.annotations),
                        _iso(alert.starts_at),
                        ends_at,
                        _iso(received_at),
                    ),
                )
                return "new"
            if row["status"] == alert.status:
                return "duplicate"
            self._conn.execute(
                "UPDATE alerts SET status=?, ends_at=? WHERE fingerprint=? AND incident_id=?",
                (alert.status, ends_at, alert.fingerprint, incident_id),
            )
            return "changed"

    def alerts(self, incident_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alerts WHERE incident_id=? ORDER BY received_at, name",
                (incident_id,),
            ).fetchall()
        return [_with_json(row, "labels", "annotations") for row in rows]

    def firing_alerts(self, incident_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT count(*) AS n FROM alerts WHERE incident_id=? AND status='firing'",
                (incident_id,),
            ).fetchone()
        return int(row["n"])

    def open_incident_for_fingerprint(self, fingerprint: str) -> Incident | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT i.* FROM incidents i JOIN alerts a ON a.incident_id = i.id "
                "WHERE a.fingerprint=? AND i.status=? ORDER BY i.opened_at DESC LIMIT 1",
                (fingerprint, OPEN),
            ).fetchone()
        return _incident(row) if row else None

    def last_closed_incident_for_fingerprint(self, fingerprint: str) -> Incident | None:
        """The most recently closed incident that included this alert."""
        with self._lock:
            row = self._conn.execute(
                "SELECT i.* FROM incidents i JOIN alerts a ON a.incident_id = i.id "
                "WHERE a.fingerprint=? AND i.status=? ORDER BY i.closed_at DESC LIMIT 1",
                (fingerprint, CLOSED),
            ).fetchone()
        return _incident(row) if row else None

    # --- evidence and diagnoses -------------------------------------------------

    def add_evidence(self, bundle: EvidenceBundle, round_number: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO evidence_bundles (incident_id, round, collected_at, bundle) "
                "VALUES (?,?,?,?)",
                (
                    bundle.incident_id,
                    round_number,
                    _iso(bundle.collected_at),
                    bundle.model_dump_json(),
                ),
            )

    def evidence(self, incident_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM evidence_bundles WHERE incident_id=? ORDER BY id", (incident_id,)
            ).fetchall()
        return [_with_json(row, "bundle") for row in rows]

    def add_diagnosis(self, incident_id: str, record: DiagnosisRecord, ts: datetime) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO diagnoses (incident_id, created_at, mode, provider, model, "
                "prompt_version, tokens_in, tokens_out, latency_ms, degraded_mode, diagnosis) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    incident_id,
                    _iso(ts),
                    record.mode,
                    record.provider,
                    record.model,
                    record.prompt_version,
                    record.tokens_in,
                    record.tokens_out,
                    record.latency_ms,
                    int(record.degraded_mode),
                    record.diagnosis.model_dump_json(),
                ),
            )

    def diagnoses(self, incident_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM diagnoses WHERE incident_id=? ORDER BY id", (incident_id,)
            ).fetchall()
        return [_with_json(row, "diagnosis") for row in rows]

    # --- actions --------------------------------------------------------------

    def add_action(
        self,
        incident_id: str,
        *,
        action_type: str,
        target: str,
        params: dict[str, Any],
        risk: str,
        decision: str,
        rule: str,
        dry_run: bool,
        status: str,
        ts: datetime,
    ) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO actions (incident_id, type, target, params, risk, decision, rule, "
                "dry_run, status, requested_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    incident_id,
                    action_type,
                    target,
                    _json(params),
                    risk,
                    decision,
                    rule,
                    int(dry_run),
                    status,
                    _iso(ts),
                ),
            )
        return int(cursor.lastrowid or 0)

    def update_action(self, action_id: int, **fields: Any) -> None:
        """Update whitelisted action columns."""
        allowed = {"status", "decision", "decided_by", "started_at", "finished_at", "outcome"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot update action columns: {sorted(unknown)}")
        values = {
            key: _iso(value)
            if isinstance(value, datetime)
            else _json(value)
            if key == "outcome"
            else value
            for key, value in fields.items()
        }
        assignments = ", ".join(f"{column}=?" for column in values)
        with self._lock:
            self._conn.execute(
                f"UPDATE actions SET {assignments} WHERE id=?",  # noqa: S608 - whitelisted columns
                (*values.values(), action_id),
            )

    def actions(self, incident_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM actions WHERE incident_id=? ORDER BY id", (incident_id,)
            ).fetchall()
        return [_with_json(row, "params", "outcome") for row in rows]

    def pending_actions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM actions WHERE status='pending_approval' ORDER BY id"
            ).fetchall()
        return [_with_json(row, "params", "outcome") for row in rows]

    def in_flight_action(self, target: str) -> dict[str, Any] | None:
        """An action currently executing (or awaiting verification) against `target`."""
        with self._lock:
            row = self._conn.execute(
                "SELECT a.* FROM actions a JOIN incidents i ON i.id = a.incident_id "
                "WHERE a.target=? AND a.dry_run=0 AND (a.status='executing' OR "
                "(a.status='executed' AND i.state='VERIFYING')) ORDER BY a.id DESC LIMIT 1",
                (target,),
            ).fetchone()
        return _with_json(row, "params", "outcome") if row else None

    def last_executed_action(self, target: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM actions WHERE target=? AND dry_run=0 AND status='executed' "
                "ORDER BY finished_at DESC LIMIT 1",
                (target,),
            ).fetchone()
        return _with_json(row, "params", "outcome") if row else None

    # --- policy limits ----------------------------------------------------------

    def add_limit_event(self, scope: str, key: str, ts: datetime) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO rate_limits (scope, key, ts) VALUES (?,?,?)", (scope, key, _iso(ts))
            )

    def count_limit_events(self, scope: str, key: str, since: datetime) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT count(*) AS n FROM rate_limits WHERE scope=? AND key=? AND ts>=?",
                (scope, key, _iso(since)),
            ).fetchone()
        return int(row["n"])

    def last_limit_event(self, scope: str, key: str) -> datetime | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT max(ts) AS ts FROM rate_limits WHERE scope=? AND key=?", (scope, key)
            ).fetchone()
        return datetime.fromisoformat(row["ts"]) if row["ts"] else None

    # --- deploys --------------------------------------------------------------

    def add_deploy(self, event: DeployEvent) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO deploys (service, version, previous_version, status, reason, "
                "incident_id, override_reason, actor, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    event.service,
                    event.version,
                    event.previous_version,
                    event.status,
                    event.reason,
                    event.incident_id,
                    event.override_reason,
                    event.actor,
                    _iso(event.ts),
                ),
            )

    def deploys(self, service: str | None = None, limit: int = 20) -> list[DeployEvent]:
        query = "SELECT * FROM deploys"
        params: list[Any] = []
        if service:
            query += " WHERE service=?"
            params.append(service)
        query += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_deploy(row) for row in rows]

    # --- tickets --------------------------------------------------------------

    def upsert_ticket(
        self,
        *,
        key: str,
        severity: str,
        priority: str,
        service: str,
        slo: str | None,
        status: str,
        summary: str,
        runbook: str,
        incident_id: str | None,
        links: dict[str, str],
        now: datetime,
    ) -> bool:
        """Create a ticket, or update status and summary of the one with this key."""
        with self._lock:
            exists = self._conn.execute("SELECT 1 FROM tickets WHERE key=?", (key,)).fetchone()
            if exists:
                self._conn.execute(
                    "UPDATE tickets SET status=?, summary=?, priority=?, updated_at=? WHERE key=?",
                    (status, summary, priority, _iso(now), key),
                )
                return False
            self._conn.execute(
                "INSERT INTO tickets (key, severity, priority, service, slo, status, summary, "
                "runbook, incident_id, links, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    severity,
                    priority,
                    service,
                    slo,
                    status,
                    summary,
                    runbook,
                    incident_id,
                    _json(links),
                    _iso(now),
                    _iso(now),
                ),
            )
            return True

    def set_ticket_status(self, key: str, status: str, now: datetime) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tickets SET status=?, updated_at=? WHERE key=?", (status, _iso(now), key)
            )

    def tickets(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tickets ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_with_json(row, "links") for row in rows]

    # --- audit log --------------------------------------------------------------

    def audit(
        self, *, actor: str, action: str, subject: str, data: dict[str, Any], ts: datetime
    ) -> None:
        """Append a tamper-evident audit row."""
        with self._lock:
            last = self._conn.execute(
                "SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev_hash = last["hash"] if last else GENESIS_HASH
            ts_text, data_text = _iso(ts), _json(data)
            self._conn.execute(
                "INSERT INTO audit_log (ts, actor, action, subject, data, prev_hash, hash) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    ts_text,
                    actor,
                    action,
                    subject,
                    data_text,
                    prev_hash,
                    audit_hash(prev_hash, ts_text, actor, action, subject, data_text),
                ),
            )

    def audit_entries(self, subject: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        query = "SELECT * FROM audit_log"
        params: list[Any] = []
        if subject:
            query += " WHERE subject=?"
            params.append(subject)
        query += " ORDER BY id LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_with_json(row, "data") for row in rows]

    def verify_audit_chain(self) -> tuple[bool, int]:
        """Recompute every hash. Returns (intact, rows checked)."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        prev_hash = GENESIS_HASH
        for row in rows:
            expected = audit_hash(
                prev_hash, row["ts"], row["actor"], row["action"], row["subject"], row["data"]
            )
            if row["prev_hash"] != prev_hash or row["hash"] != expected:
                return False, len(rows)
            prev_hash = row["hash"]
        return True, len(rows)

    # --- runtime state ----------------------------------------------------------

    def get_state(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM agent_state WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_state (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                (key, value),
            )


def _statements(sql: str) -> list[str]:
    """Split a migration file into statements, dropping `--` comment lines."""
    lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    return [statement.strip() for statement in "\n".join(lines).split(";") if statement.strip()]
