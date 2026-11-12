"""Agent metrics (`agent_*`). The agent is monitored like any production service."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

MODES = ("observe", "dry_run", "enforce")
INCIDENT_BUCKETS = (5, 15, 30, 60, 120, 300, 600, 1800)
DIAGNOSIS_BUCKETS = (0.01, 0.05, 0.25, 1, 2.5, 5, 10, 30)
LLM_BUCKETS = (0.25, 0.5, 1, 2, 4, 8, 16, 32)


class AgentMetrics:
    """Owns the agent's metric objects, registered on the application registry."""

    def __init__(self, registry: CollectorRegistry) -> None:
        def counter(name: str, text: str, labels: list[str] | None = None) -> Counter:
            return Counter(name, text, labels or [], registry=registry)

        self.alerts_received = counter(
            "agent_alerts_received_total",
            "Alerts received from Alertmanager, by severity label.",
            ["severity"],
        )
        self.incidents = counter(
            "agent_incidents_total", "Incidents by the state they reached.", ["status"]
        )
        self.incident_duration = Histogram(
            "agent_incident_duration_seconds",
            "Time from incident creation to its final state.",
            buckets=INCIDENT_BUCKETS,
            registry=registry,
        )
        self.diagnoses = counter(
            "agent_diagnosis_total", "Diagnoses by mode and category.", ["mode", "category"]
        )
        self.diagnosis_duration = Histogram(
            "agent_diagnosis_duration_seconds",
            "Time to produce a diagnosis, including evidence requests.",
            ["mode"],
            buckets=DIAGNOSIS_BUCKETS,
            registry=registry,
        )
        self.llm_calls = counter(
            "agent_llm_calls_total", "LLM calls by provider and outcome.", ["provider", "outcome"]
        )
        self.llm_tokens = counter(
            "agent_llm_tokens_total", "LLM tokens by direction (in or out).", ["direction"]
        )
        self.llm_latency = Histogram(
            "agent_llm_latency_seconds",
            "Latency of LLM calls.",
            buckets=LLM_BUCKETS,
            registry=registry,
        )
        self.llm_errors = counter("agent_llm_errors_total", "LLM errors by kind.", ["kind"])
        self.llm_circuit_state = Gauge(
            "agent_llm_circuit_state",
            "LLM circuit breaker: 0 closed, 1 open.",
            registry=registry,
        )
        self.validation_failures = counter(
            "agent_validation_failures_total",
            "Diagnoses rejected by the validator, by reason.",
            ["reason"],
        )
        self.actions = counter(
            "agent_actions_total",
            "Actions by type and outcome (executed, blocked, pending_approval, rejected, "
            "dry_run, failed).",
            ["action", "outcome"],
        )
        self.recovery_verified = counter(
            "agent_recovery_verified_total",
            "Verification results after an action (recovered or failed).",
            ["result"],
        )
        self.escalations = counter(
            "agent_escalations_total", "Incidents escalated to a human, by reason.", ["reason"]
        )
        self.queue_depth = Gauge(
            "agent_queue_depth", "Incidents waiting for a worker.", registry=registry
        )
        self.queue_rejected = counter(
            "agent_queue_rejected_total", "Webhooks rejected because the queue was full."
        )
        self.mode = Gauge(
            "agent_mode", "1 for the active agent mode, 0 otherwise.", ["mode"], registry=registry
        )

    def set_mode(self, active: str) -> None:
        for mode in MODES:
            self.mode.labels(mode=mode).set(1 if mode == active else 0)
