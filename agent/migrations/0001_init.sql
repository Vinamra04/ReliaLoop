-- Agent store, version 1. Timestamps are ISO-8601 UTC strings; JSON columns hold text.

CREATE TABLE incidents (
    id                 TEXT PRIMARY KEY,
    service            TEXT NOT NULL,
    slo                TEXT,
    severity           TEXT NOT NULL,
    state              TEXT NOT NULL,
    status             TEXT NOT NULL,
    mode               TEXT NOT NULL,
    diagnosis_mode     TEXT NOT NULL,
    category           TEXT,
    root_cause_service TEXT,
    degraded_mode      INTEGER NOT NULL DEFAULT 0,
    replans            INTEGER NOT NULL DEFAULT 0,
    summary            TEXT NOT NULL DEFAULT '',
    resolution         TEXT,
    escalation_reason  TEXT,
    opened_at          TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    closed_at          TEXT
);
CREATE INDEX incidents_service_idx ON incidents (service, status);
CREATE INDEX incidents_opened_idx ON incidents (opened_at);

CREATE TABLE incident_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL REFERENCES incidents (id),
    ts          TEXT NOT NULL,
    state       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    message     TEXT NOT NULL,
    data        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX incident_events_incident_idx ON incident_events (incident_id, id);

CREATE TABLE alerts (
    fingerprint TEXT NOT NULL,
    incident_id TEXT NOT NULL REFERENCES incidents (id),
    name        TEXT NOT NULL,
    status      TEXT NOT NULL,
    labels      TEXT NOT NULL,
    annotations TEXT NOT NULL,
    starts_at   TEXT NOT NULL,
    ends_at     TEXT,
    received_at TEXT NOT NULL,
    PRIMARY KEY (fingerprint, incident_id)
);
CREATE INDEX alerts_incident_idx ON alerts (incident_id);

CREATE TABLE evidence_bundles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id  TEXT NOT NULL REFERENCES incidents (id),
    round        INTEGER NOT NULL,
    collected_at TEXT NOT NULL,
    bundle       TEXT NOT NULL
);
CREATE INDEX evidence_incident_idx ON evidence_bundles (incident_id, id);

CREATE TABLE diagnoses (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id    TEXT NOT NULL REFERENCES incidents (id),
    created_at     TEXT NOT NULL,
    mode           TEXT NOT NULL,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    tokens_in      INTEGER NOT NULL,
    tokens_out     INTEGER NOT NULL,
    latency_ms     REAL NOT NULL,
    degraded_mode  INTEGER NOT NULL,
    diagnosis      TEXT NOT NULL
);
CREATE INDEX diagnoses_incident_idx ON diagnoses (incident_id, id);

CREATE TABLE actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id  TEXT NOT NULL REFERENCES incidents (id),
    type         TEXT NOT NULL,
    target       TEXT NOT NULL,
    params       TEXT NOT NULL,
    risk         TEXT NOT NULL,
    decision     TEXT NOT NULL,
    rule         TEXT NOT NULL,
    dry_run      INTEGER NOT NULL,
    status       TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    decided_by   TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    outcome      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX actions_incident_idx ON actions (incident_id, id);
CREATE INDEX actions_target_idx ON actions (target, status, started_at);

CREATE TABLE deploys (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    service          TEXT NOT NULL,
    version          TEXT NOT NULL,
    previous_version TEXT,
    status           TEXT NOT NULL,
    reason           TEXT NOT NULL,
    incident_id      TEXT,
    override_reason  TEXT,
    actor            TEXT NOT NULL,
    ts               TEXT NOT NULL
);
CREATE INDEX deploys_service_idx ON deploys (service, ts);

CREATE TABLE tickets (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    key               TEXT NOT NULL UNIQUE,
    severity          TEXT NOT NULL,
    priority          TEXT NOT NULL,
    service           TEXT NOT NULL,
    slo               TEXT,
    status            TEXT NOT NULL,
    summary           TEXT NOT NULL,
    runbook           TEXT NOT NULL DEFAULT '',
    incident_id       TEXT,
    links             TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX tickets_service_idx ON tickets (service, status);

-- Append-only. Each row stores the previous row's hash and its own sha256,
-- so any edit or deletion breaks the chain and is detectable.
CREATE TABLE audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    subject   TEXT NOT NULL,
    data      TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL
);

-- One row per event that counts against a policy limit.
CREATE TABLE rate_limits (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    key   TEXT NOT NULL,
    ts    TEXT NOT NULL
);
CREATE INDEX rate_limits_idx ON rate_limits (scope, key, ts);

CREATE TABLE agent_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
