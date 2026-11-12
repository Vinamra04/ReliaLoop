"""Evidence collection.

Builds the `EvidenceBundle` for an incident from the catalog queries, the
logs, the deploy history and the container runtime. No LLM is involved, and a
failing source never fails the incident: the bundle is marked incomplete and
the missing source is itself recorded as evidence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from agent.app.config import AgentSettings
from agent.app.domain import (
    ContainerStatus,
    DeployEvent,
    EvidenceBundle,
    EvidenceItem,
    LogSummary,
)
from agent.evidence.catalog import Catalog, CatalogQuery
from agent.evidence.log_summary import summarize_logs
from agent.ports.protocols import (
    ActionExecutor,
    DeployHistory,
    LogSource,
    MetricsSource,
    SourceError,
)
from libs.core.cmdb import Cmdb

Clock = Callable[[], datetime]
RECENT_START_S = 300


def utc_now() -> datetime:
    return datetime.now(UTC)


def item_id(name: str, service: str, suffix: str = "") -> str:
    """Stable evidence id, e.g. `err_ratio_1m@orders` or `burn_rate_2m:orders-latency@orders`."""
    return f"{name}:{suffix}@{service}" if suffix else f"{name}@{service}"


class EvidenceCollector:
    """Collects evidence for the alerting service and everything it depends on."""

    def __init__(
        self,
        *,
        settings: AgentSettings,
        cmdb: Cmdb,
        catalog: Catalog,
        metrics: MetricsSource,
        logs: LogSource,
        deploys: DeployHistory,
        executor: ActionExecutor,
        clock: Clock = utc_now,
    ) -> None:
        self._settings = settings
        self._cmdb = cmdb
        self._catalog = catalog
        self._metrics = metrics
        self._logs = logs
        self._deploys = deploys
        self._executor = executor
        self._clock = clock

    def _observable(self, service: str) -> list[str]:
        """Managed services in the dependency chain: the ones that emit evidence."""
        return [
            name
            for name in self._cmdb.dependency_chain(service)
            if self._cmdb.services[name].managed
        ]

    async def collect(self, incident_id: str, service: str, slo: str | None) -> EvidenceBundle:
        """Collect the core evidence set, bounded by the overall evidence timeout."""
        services = self._observable(service)
        now = self._clock()
        items: list[EvidenceItem] = []
        incomplete: list[str] = []
        log_summaries: list[LogSummary] = []
        deploy_events: list[DeployEvent] = []

        async def gather() -> None:
            jobs = [
                self._query(name, query, target, items, incomplete)
                for target in services
                for name, query in self._catalog.core().items()
                if query.applies_to(target)
            ]
            jobs += [self._runtime(target, items, incomplete) for target in services]
            jobs += [
                self._log_summary(target, items, log_summaries, incomplete) for target in services
            ]
            await asyncio.gather(*jobs)

        try:
            await asyncio.wait_for(gather(), timeout=self._settings.evidence_timeout_s)
        except TimeoutError:
            incomplete.append("timeout")
        for target in services:
            deploy_events.extend(self._deploy_evidence(target, items, incomplete))
        for source in sorted(set(incomplete)):
            items.append(
                EvidenceItem(
                    id=item_id("source_unavailable", service, source),
                    service=service,
                    name="source_unavailable",
                    value=source,
                    unit="source",
                    observed_at=now,
                    source="agent",
                )
            )
        return EvidenceBundle(
            incident_id=incident_id,
            alerting_service=service,
            slo=slo,
            services=tuple(services),
            collected_at=now,
            items=tuple(sorted(items, key=lambda item: (item.service, item.id))),
            logs=tuple(sorted(log_summaries, key=lambda summary: summary.service)),
            deploys=tuple(deploy_events),
            incomplete=tuple(sorted(set(incomplete))),
        )

    async def collect_extra(self, bundle: EvidenceBundle, names: list[str]) -> EvidenceBundle:
        """Add optional catalog entries (by name) to a bundle; unknown names are ignored."""
        items: list[EvidenceItem] = []
        incomplete: list[str] = list(bundle.incomplete)
        known = [name for name in names if name in self._catalog.queries]
        jobs = [
            self._query(name, self._catalog.get(name), target, items, incomplete)
            for name in known
            for target in bundle.services
            if self._catalog.get(name).applies_to(target)
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*jobs), timeout=self._settings.evidence_timeout_s)
        except TimeoutError:
            incomplete.append("timeout")
        existing = bundle.ids()
        merged = [*bundle.items, *(item for item in items if item.id not in existing)]
        return bundle.model_copy(
            update={
                "items": tuple(sorted(merged, key=lambda item: (item.service, item.id))),
                "incomplete": tuple(sorted(set(incomplete))),
            }
        )

    async def _query(
        self,
        name: str,
        query: CatalogQuery,
        service: str,
        items: list[EvidenceItem],
        incomplete: list[str],
    ) -> None:
        now = self._clock()
        try:
            samples = await self._metrics.instant(query.render(service))
        except SourceError:
            incomplete.append("prometheus")
            return

        def make(value: float | str | None, suffix: str = "") -> EvidenceItem:
            return EvidenceItem(
                id=item_id(name, service, suffix),
                service=service,
                name=name,
                window=query.window,
                value=value,
                unit=query.unit,
                observed_at=now,
            )

        if query.expand_by:
            for sample in samples:
                suffix = ",".join(sample.labels.get(label, "") for label in query.expand_by)
                items.append(make(sample.value, suffix))
        elif query.value_label:
            versions = sorted({s.labels.get(query.value_label, "") for s in samples} - {""})
            items.append(make(",".join(versions) if versions else None))
        else:
            items.append(make(samples[0].value if samples else None))

    async def _runtime(
        self, service: str, items: list[EvidenceItem], incomplete: list[str]
    ) -> None:
        """Container facts from the runtime: they exist even when metrics are gone."""
        now = self._clock()
        try:
            containers = await self._executor.status(service)
        except SourceError:
            incomplete.append("runtime")
            return
        recent = [c for c in containers if _started_recently(c, now)]
        facts: dict[str, float] = {
            "containers_running": sum(1 for c in containers if c.state == "running"),
            "containers_unhealthy": sum(
                1 for c in containers if c.state != "running" or c.health == "unhealthy"
            ),
            # Restarts by the restart policy, counted only for containers that
            # (re)started recently so an old crash does not look like a new one.
            "container_restarts": max((c.restart_count for c in recent), default=0),
            "container_oom_killed": sum(1 for c in containers if c.oom_killed),
        }
        items.extend(
            EvidenceItem(
                id=item_id(name, service),
                service=service,
                name=name,
                value=float(value),
                unit="count",
                observed_at=now,
                source="runtime",
            )
            for name, value in facts.items()
        )

    async def _log_summary(
        self,
        service: str,
        items: list[EvidenceItem],
        summaries: list[LogSummary],
        incomplete: list[str],
    ) -> None:
        now = self._clock()
        try:
            lines = await self._logs.recent(
                service, self._settings.log_lines, self._settings.log_since_s
            )
        except SourceError:
            incomplete.append("logs")
            return
        summary = summarize_logs(
            service,
            lines,
            max_bytes=self._settings.log_max_bytes,
            tail_lines=self._settings.log_tail_lines,
        )
        summaries.append(summary)
        items.append(
            EvidenceItem(
                id=item_id("log_error_lines", service),
                service=service,
                name="log_error_lines",
                window=f"{self._settings.log_since_s}s",
                value=float(sum(count for _, count in summary.error_templates)),
                unit="count",
                observed_at=now,
                source="logs",
            )
        )

    def _deploy_evidence(
        self, service: str, items: list[EvidenceItem], incomplete: list[str]
    ) -> list[DeployEvent]:
        now = self._clock()
        try:
            history = self._deploys.deploys(service, self._settings.deploy_history_limit)
        except Exception:  # the store must never take an incident down
            incomplete.append("deploy_history")
            return []
        # Rollbacks done by the agent are not "a deploy that might have broken things".
        releases = [event for event in history if event.reason != "rollback"]
        latest = releases[0] if releases else None
        values: dict[str, float | str | None] = {
            "seconds_since_last_deploy": (now - latest.ts).total_seconds() if latest else None,
            "last_deploy_version": latest.version if latest else None,
            "previous_version": latest.previous_version if latest else None,
        }
        items.extend(
            EvidenceItem(
                id=item_id(name, service),
                service=service,
                name=name,
                value=value,
                unit="seconds" if name == "seconds_since_last_deploy" else "version",
                observed_at=now,
                source="deploy_history",
            )
            for name, value in values.items()
        )
        return history


def _started_recently(container: ContainerStatus, now: datetime) -> bool:
    try:
        started = datetime.fromisoformat(container.started_at[:19]).replace(tzinfo=UTC)
    except ValueError:
        return False
    return now - started <= timedelta(seconds=RECENT_START_S)
