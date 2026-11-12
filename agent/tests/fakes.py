"""In-memory fakes for the agent's ports, shared by the unit tests."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from agent.app.config import AgentSettings
from agent.app.domain import ActionResult, ContainerStatus, DeployEvent
from agent.evidence.catalog import Catalog, load_catalog
from agent.ports.protocols import Sample, SourceError
from agent.tests.fixtures import APPROVER_TOKEN, WEBHOOK_TOKEN
from libs.core.cmdb import Cmdb, load_cmdb
from tools import paths

T0 = datetime(2026, 11, 12, 8, 0, tzinfo=UTC)
SERVICES = ("gateway", "orders")

# A quiet, healthy system: the baseline every test scenario starts from.
HEALTHY: dict[str, float | str] = {
    "err_ratio_1m": 0.0,
    "err_ratio_5m": 0.0,
    "p99_latency_1m": 0.12,
    "request_rate_1m": 20.0,
    "request_rate_baseline": 20.0,
    "in_flight": 2.0,
    "worker_utilization": 0.25,
    "rss_bytes": 60 * 1024 * 1024,
    "rss_pct_of_limit": 0.31,
    "rss_slope_1m": 0.0,
    "restarts_2m": 0.0,
    "replicas_up": 1.0,
    "version": "v1",
}


def settings(**overrides: Any) -> AgentSettings:
    defaults: dict[str, Any] = {
        "webhook_token": WEBHOOK_TOKEN,
        "approver_token": APPROVER_TOKEN,
        "agent_mode": "enforce",
        "verify_wait_s": 0,
        "verify_interval_s": 0.001,
        "verify_timeout_s": 0.05,
        "runbooks_dir": paths.REPO_ROOT / "docs" / "runbooks",
        "cmdb_path": paths.CMDB_FILE,
        "policy_path": paths.REPO_ROOT / "agent" / "policy.yaml",
    }
    return AgentSettings(**{**defaults, **overrides})


def cmdb() -> Cmdb:
    return load_cmdb(paths.CMDB_FILE)


class Clock:
    """Controllable clock."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeMetrics:
    """Answers catalog queries from a table of `(name, service) -> value`."""

    def __init__(self, catalog: Catalog | None = None) -> None:
        self.catalog = catalog or load_catalog()
        self.values: dict[tuple[str, str], Any] = {}
        self.raw: dict[str, list[Sample]] = {}
        self.fail = False
        self.queries: list[str] = []
        self._reverse = {
            query.render(service): (name, service)
            for name, query in self.catalog.queries.items()
            for service in SERVICES
        }
        for service in SERVICES:
            self.set_many(service, HEALTHY)

    def set(self, name: str, service: str, value: Any) -> None:
        """Value may be a number, text (for `value_label` queries), a dict
        `{label_value: number}` for expanded queries, or None for no data."""
        self.values[(name, service)] = value

    def set_many(self, service: str, values: Mapping[str, Any]) -> None:
        for name, value in values.items():
            self.set(name, service, value)

    async def instant(self, query: str) -> list[Sample]:
        self.queries.append(query)
        if self.fail:
            raise SourceError("prometheus unreachable: ConnectError")
        if query in self.raw:
            return self.raw[query]
        key = self._reverse.get(query)
        if key is None or key not in self.values or self.values[key] is None:
            return []
        name, _ = key
        spec = self.catalog.queries[name]
        value = self.values[key]
        if spec.expand_by:
            return [
                Sample(labels=dict(zip(spec.expand_by, label.split(","), strict=True)), value=v)
                for label, v in value.items()
            ]
        if spec.value_label:
            return [Sample(labels={spec.value_label: value}, value=1.0)]
        return [Sample(labels={}, value=float(value))]


class FakeLogs:
    def __init__(self) -> None:
        self.lines: dict[str, list[str]] = {}
        self.fail = False

    async def recent(self, service: str, lines: int, since_s: int) -> list[str]:
        if self.fail:
            raise SourceError("docker unreachable")
        return self.lines.get(service, [])[-lines:]


class FakeDeploys:
    def __init__(self) -> None:
        self.events: list[DeployEvent] = []

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
        self.events.append(
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
        matching = [e for e in self.events if service is None or e.service == service]
        return sorted(matching, key=lambda e: e.ts, reverse=True)[:limit]


def container(
    name: str = "relialoop-orders-1",
    *,
    image: str = "relialoop/orders:v1",
    state: str = "running",
    health: str = "healthy",
    restart_count: int = 0,
    started_at: datetime = T0 - timedelta(hours=1),
    oom_killed: bool = False,
) -> ContainerStatus:
    return ContainerStatus(
        name=name,
        image=image,
        state=state,
        health=health,
        restart_count=restart_count,
        started_at=started_at.isoformat(),
        oom_killed=oom_killed,
    )


class FakeExecutor:
    """Records every action and returns configurable results."""

    def __init__(self) -> None:
        self.containers: dict[str, list[ContainerStatus]] = {
            "gateway": [container("relialoop-gateway-1", image="relialoop/gateway:v1")],
            "orders": [container()],
        }
        self.calls: list[tuple[str, str, Any]] = []
        self.results: dict[str, ActionResult] = {}
        self.available_versions: set[tuple[str, str]] = {("orders", "v1"), ("gateway", "v1")}
        self.fail_status = False
        self.raise_on: set[str] = set()

    def _result(self, action: str) -> ActionResult:
        if action in self.raise_on:
            raise SourceError("docker unreachable")
        return self.results.get(action, ActionResult(success=True, detail=f"{action} ok"))

    async def status(self, service: str) -> list[ContainerStatus]:
        if self.fail_status:
            raise SourceError("docker unreachable")
        return self.containers.get(service, [])

    async def restart(self, service: str) -> ActionResult:
        self.calls.append(("restart", service, None))
        return self._result("restart")

    async def scale(self, service: str, replicas: int) -> ActionResult:
        self.calls.append(("scale", service, replicas))
        return self._result("scale")

    async def rollback(self, service: str, to_version: str) -> ActionResult:
        self.calls.append(("rollback", service, to_version))
        return self._result("rollback")

    async def version_available(self, service: str, version: str) -> bool:
        return (service, version) in self.available_versions


class FakeNotifier:
    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, Any]]] = []

    async def publish(self, kind: str, payload: Mapping[str, Any]) -> None:
        self.events.append((kind, payload))


class EvidenceRig:
    """All evidence fakes wired to a real collector."""

    def __init__(self, **overrides: Any) -> None:
        from agent.evidence.collector import EvidenceCollector

        self.settings = settings(**overrides)
        self.cmdb = cmdb()
        self.clock = Clock()
        self.metrics = FakeMetrics()
        self.logs = FakeLogs()
        self.deploys = FakeDeploys()
        self.executor = FakeExecutor()
        self.collector = EvidenceCollector(
            settings=self.settings,
            cmdb=self.cmdb,
            catalog=self.metrics.catalog,
            metrics=self.metrics,
            logs=self.logs,
            deploys=self.deploys,
            executor=self.executor,
            clock=self.clock,
        )

    async def bundle(self, service: str = "gateway", slo: str | None = "gateway-availability"):
        return await self.collector.collect("INC-20261112-001", service, slo)

    # --- fault scenarios, as they look in the evidence ---------------------------

    def error_spike(self, service: str = "orders", ratio: float = 0.5) -> None:
        self.metrics.set_many(service, {"err_ratio_1m": ratio, "err_ratio_5m": ratio / 2})
        if service == "orders":
            self.metrics.set_many("gateway", {"err_ratio_1m": ratio, "err_ratio_5m": ratio / 2})

    def saturation(self) -> None:
        self.metrics.set_many(
            "orders",
            {
                "worker_utilization": 0.99,
                "request_rate_1m": 150.0,
                "request_rate_baseline": 20.0,
                "err_ratio_1m": 0.45,
                "p99_latency_1m": 1.9,
            },
        )
        self.metrics.set_many(
            "gateway",
            {
                "request_rate_1m": 150.0,
                "request_rate_baseline": 20.0,
                "err_ratio_1m": 0.45,
                "p99_latency_1m": 1.95,
            },
        )

    def crash_loop(self, *, deployed: bool = True) -> None:
        self.metrics.set_many(
            "orders",
            {"restarts_2m": 5.0, "replicas_up": 0.0, "err_ratio_1m": None, "version": "v3-crash"},
        )
        self.metrics.set_many("gateway", {"err_ratio_1m": 0.9})
        self.executor.containers["orders"] = [
            container(
                image="relialoop/orders:v3-crash",
                restart_count=5,
                started_at=self.clock.now - timedelta(seconds=4),
                health="starting",
            )
        ]
        if deployed:
            self.deploys.add("orders", "v3-crash", "v1", self.clock.now - timedelta(seconds=90))

    def memory_leak(self, service: str = "gateway") -> None:
        self.metrics.set_many(
            service,
            {
                "rss_pct_of_limit": 0.75,
                "rss_slope_1m": 1_048_576.0,
                "rss_bytes": 144 * 1024 * 1024,
                "alert_firing": {"MemoryExhaustionPredicted": 1.0},
            },
        )

    def bad_deploy(self) -> None:
        self.metrics.set_many(
            "orders", {"err_ratio_1m": 0.26, "p99_latency_1m": 0.49, "version": "v2-bad"}
        )
        self.metrics.set_many("gateway", {"err_ratio_1m": 0.27})
        self.deploys.add("orders", "v2-bad", "v1", self.clock.now - timedelta(seconds=120))
