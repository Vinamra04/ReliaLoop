"""Agent metrics (`agent_*`). The agent is monitored like any production service."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter


class AgentMetrics:
    """Owns the agent's metric objects, registered on the application registry."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self.alerts_received = Counter(
            "agent_alerts_received_total",
            "Alerts received from Alertmanager, by severity label.",
            ["severity"],
            registry=registry,
        )
