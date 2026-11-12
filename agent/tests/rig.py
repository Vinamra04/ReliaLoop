"""A complete agent pipeline wired to fakes, with a real SQLite store."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from prometheus_client import CollectorRegistry, generate_latest

from agent.adapters.sqlite_store import SqliteStore
from agent.app.api import Components
from agent.app.domain import DeployEvent, Incident
from agent.app.metrics import AgentMetrics
from agent.app.models import AmWebhook
from agent.app.pipeline import IncidentPipeline
from agent.diagnosis.engine import Diagnoser, RulesDiagnoser
from agent.policy.engine import PolicyEngine
from agent.policy.limits import load_policy
from agent.ports.protocols import Sample
from agent.tests.fakes import EvidenceRig, FakeNotifier
from agent.tests.fixtures import alert, webhook
from agent.verify.verifier import Verifier

SLOS = ("gateway-availability", "gateway-latency", "orders-availability", "orders-latency")
BURN_QUERY = 'slo:burn_rate:ratio_30s{slo="%s"}'


async def _no_sleep(_: float) -> None:
    return None


class StoreDeploys:
    """Deploy history backed by the store, with the same `add` helper as the fake."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def add(
        self,
        service: str,
        version: str,
        previous: str | None,
        ts: datetime,
        *,
        reason: str = "deploy",
        status: str = "completed",
    ) -> None:
        self._store.add_deploy(
            DeployEvent(
                service=service,
                version=version,
                previous_version=previous,
                status=status,
                reason=reason,
                ts=ts,
            )
        )

    def deploys(self, service: str | None = None, limit: int = 20) -> list[DeployEvent]:
        return self._store.deploys(service, limit)


class PipelineRig(EvidenceRig):
    """Evidence fakes plus store, policy, verifier and the pipeline itself."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        diagnoser: Diagnoser | None = None,
        metrics: AgentMetrics | None = None,
        **overrides: Any,
    ) -> None:
        super().__init__(**overrides)
        self.db_path = tmp_path / "agent.db"
        self.store = SqliteStore(self.db_path)
        self.store.migrate()
        # Deploy history comes from the real store here, as in production.
        self.deploys = StoreDeploys(self.store)  # type: ignore[assignment]
        self.collector._deploys = self.deploys
        self.notifier = FakeNotifier()
        self.registry = CollectorRegistry()
        self.agent_metrics = metrics or AgentMetrics(self.registry)
        self.policy = PolicyEngine(load_policy(self.settings.policy_path), self.cmdb, self.store)
        self.verifier = Verifier(
            settings=self.settings,
            cmdb=self.cmdb,
            catalog=self.metrics.catalog,
            metrics=self.metrics,
            executor=self.executor,
            sleep=_no_sleep,
        )
        self.pipeline = self.new_pipeline(diagnoser)
        self.recovered(True)

    def new_pipeline(self, diagnoser: Diagnoser | None = None) -> IncidentPipeline:
        """A pipeline over the same store: also used to simulate an agent restart."""
        return IncidentPipeline(
            settings=self.settings,
            cmdb=self.cmdb,
            store=self.store,
            collector=self.collector,
            diagnosers=self._diagnosers(diagnoser),
            policy=self.policy,
            executor=self.executor,
            verifier=self.verifier,
            notifier=self.notifier,
            metrics=self.agent_metrics,
            clock=self.clock,
        )

    def _diagnosers(self, diagnoser: Diagnoser | None) -> dict[str, Diagnoser]:
        """A custom diagnoser stands in for both engines; otherwise only rules exists."""
        rules: Diagnoser = RulesDiagnoser(self.settings)
        return {"rules": diagnoser or rules, "llm": diagnoser or rules}

    def recovered(self, healthy: bool) -> None:
        """Make verification see a recovered (or still burning) system."""
        for slo in SLOS:
            self.metrics.raw[BURN_QUERY % slo] = [
                Sample(labels={}, value=0.0 if healthy else 100.0)
            ]

    async def page(self, *alerts: dict[str, Any]) -> None:
        await self.pipeline.handle_page(AmWebhook.model_validate(webhook(*alerts)))

    async def run(self, *alerts: dict[str, Any]) -> None:
        """Deliver a page and process everything it triggers."""
        await self.page(*alerts)
        await self.settle()

    async def settle(self) -> None:
        await self.pipeline.start()
        try:
            await self.pipeline.drain()
        finally:
            await self.pipeline.stop()

    def incident(self, index: int = 0) -> Incident:
        """Incident by creation order."""
        return sorted(self.store.list_incidents(), key=lambda i: i.id)[index]

    def states(self, incident_id: str) -> list[str]:
        return [e["state"] for e in self.store.events(incident_id) if e["kind"] == "state"]

    def metric(self, name: str, **labels: str) -> float:
        """Current value of an agent metric sample (0 when it has never been set)."""
        rendered = ",".join(f'{key}="{value}"' for key, value in sorted(labels.items()))
        wanted = f"{name}{{{rendered}}}" if labels else name
        for line in generate_latest(self.registry).decode().splitlines():
            if line.startswith(wanted + " "):
                return float(line.rsplit(" ", 1)[1])
        return 0.0

    def components(self) -> Components:
        async def ready() -> dict[str, str]:
            return {}

        async def close() -> None:
            return None

        return Components(store=self.store, pipeline=self.pipeline, readiness=ready, close=close)


def gateway_alert(**overrides: Any) -> dict[str, Any]:
    base = {"service": "gateway", "slo": "gateway-availability", "fingerprint": "fp-gateway"}
    return alert(**{**base, **overrides})


def orders_alert(**overrides: Any) -> dict[str, Any]:
    base = {"service": "orders", "slo": "orders-availability", "fingerprint": "fp-orders"}
    return alert(**{**base, **overrides})
