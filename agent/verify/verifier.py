"""Recovery verification.

An action is not a fix until the symptoms are gone. After an action the agent
waits, then polls. The incident counts as recovered only when, for several
consecutive checks:

- the short-window burn rate of the affected SLO(s) is below half of the
  fast-burn factor (the SLI is back within its objective);
- every container of the target service is running and healthy;
- for a crash loop, no container restarted between checks;
- for a memory leak, memory is back below the leak threshold.

Otherwise verification fails and the pipeline re-plans once or escalates.
There are no blind retries.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from agent.adapters.docker_exec import is_healthy
from agent.app.config import AgentSettings
from agent.app.domain import Category, Incident, VerificationResult
from agent.evidence.catalog import Catalog
from agent.ports.protocols import ActionExecutor, MetricsSource, SourceError
from libs.core import naming
from libs.core.cmdb import Cmdb


class Verifier:
    def __init__(
        self,
        *,
        settings: AgentSettings,
        cmdb: Cmdb,
        catalog: Catalog,
        metrics: MetricsSource,
        executor: ActionExecutor,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._cmdb = cmdb
        self._catalog = catalog
        self._metrics = metrics
        self._executor = executor
        self._sleep = sleep

    def _slos(self, incident: Incident) -> list[str]:
        """The incident's SLO, or every SLO of its service when the alert had none."""
        if incident.slo:
            return [incident.slo]
        service = self._cmdb.services.get(incident.service)
        return list(service.slos) if service else []

    async def _check(
        self,
        incident: Incident,
        target: str,
        category: Category | None,
        restart_counts: dict[str, int],
    ) -> str | None:
        """One verification check. Returns None when healthy, else what is still wrong."""
        limit = self._settings.fast_burn_factor * self._settings.recovery_burn_fraction
        series = naming.burn_rate(self._settings.verify_burn_window)
        for slo in self._slos(incident):
            samples = await self._metrics.instant(f'{series}{{slo="{slo}"}}')
            if not samples:
                return f"no SLI data for {slo} (no traffic or service down)"
            if samples[0].value >= limit:
                return f"{slo} burn rate {samples[0].value:.1f} is not below {limit:.1f}"

        statuses = await self._executor.status(target)
        if not statuses:
            return f"{target} has no containers"
        unhealthy = [f"{s.name}={s.state}/{s.health}" for s in statuses if not is_healthy(s)]
        if unhealthy:
            return f"unhealthy containers: {', '.join(unhealthy)}"

        if category is Category.CRASH_LOOP:
            current = {s.name: s.restart_count for s in statuses}
            changed = [
                name
                for name, count in current.items()
                if name in restart_counts and count != restart_counts[name]
            ]
            restart_counts.clear()
            restart_counts.update(current)
            if changed:
                return f"containers restarted again: {', '.join(changed)}"

        if category is Category.MEMORY_LEAK:
            query = self._catalog.get("rss_pct_of_limit").render(target)
            samples = await self._metrics.instant(query)
            if samples and samples[0].value >= self._settings.memory_pct_threshold:
                return f"{target} memory still at {samples[0].value:.0%} of its limit"
        return None

    async def verify(
        self, incident: Incident, target: str, category: Category | None
    ) -> VerificationResult:
        """Poll until recovered for N consecutive checks, or until the timeout."""
        cfg = self._settings
        started = time.monotonic()
        await self._sleep(cfg.verify_wait_s)
        deadline = started + cfg.verify_wait_s + cfg.verify_timeout_s
        restart_counts: dict[str, int] = {}
        consecutive = 0
        checks = 0
        problem = "no check completed"
        while True:
            checks += 1
            try:
                failure = await self._check(incident, target, category, restart_counts)
            except SourceError as exc:
                failure = f"could not check: {exc}"
            if failure is None:
                consecutive += 1
                if consecutive >= cfg.verify_consecutive:
                    return VerificationResult(
                        recovered=True,
                        checks=checks,
                        detail=f"healthy for {consecutive} consecutive checks",
                        duration_s=round(time.monotonic() - started, 2),
                    )
            else:
                consecutive = 0
                problem = failure
            if time.monotonic() >= deadline:
                return VerificationResult(
                    recovered=False,
                    checks=checks,
                    detail=f"not recovered after {cfg.verify_timeout_s:.0f}s: {problem}",
                    duration_s=round(time.monotonic() - started, 2),
                )
            await self._sleep(cfg.verify_interval_s)
