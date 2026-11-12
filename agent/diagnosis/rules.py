"""Rule-based diagnosis: the deterministic baseline.

A transparent decision tree over the evidence bundle (never over alert names
alone):

1. Localise: the most downstream service in the dependency chain that shows
   anomalous signals. Symptoms appear at the gateway; causes usually sit below.
2. restarts >= 2, or one crash right after a deploy  -> CRASH_LOOP
3. deploy in the last 10 min and errors/latency up  -> BAD_DEPLOY
4. memory rising and above 70% of the limit
   (or exhaustion predicted)                        -> MEMORY_LEAK
5. workers >= 90% busy and load >= 1.5x baseline    -> LOAD_SATURATION
6. error ratio elevated                             -> ERROR_SPIKE
7. otherwise                                        -> UNKNOWN (escalate)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.app.config import AgentSettings
from agent.app.domain import (
    ActionType,
    Category,
    Citation,
    Diagnosis,
    EvidenceBundle,
    ProposedAction,
)
from agent.evidence.collector import item_id

MEMORY_ALERT = "MemoryExhaustionPredicted"

CONFIDENCE = {
    Category.CRASH_LOOP: 0.9,
    Category.BAD_DEPLOY: 0.85,
    Category.MEMORY_LEAK: 0.85,
    Category.LOAD_SATURATION: 0.85,
    Category.ERROR_SPIKE: 0.7,
    Category.UNKNOWN: 0.3,
}


@dataclass
class Signals:
    """The evidence the tree looks at for one service, with the ids it came from."""

    service: str
    bundle: EvidenceBundle
    used: dict[str, str] = field(default_factory=dict)

    def number(self, name: str, suffix: str = "") -> float | None:
        identifier = item_id(name, self.service, suffix)
        for item in self.bundle.items:
            if item.id == identifier and isinstance(item.value, int | float):
                return float(item.value)
        return None

    def text(self, name: str) -> str | None:
        return self.bundle.text(name, self.service)

    def cite(self, name: str, observation: str, suffix: str = "") -> None:
        identifier = item_id(name, self.service, suffix)
        if identifier in self.bundle.ids():
            self.used[identifier] = observation

    def citations(self) -> tuple[Citation, ...]:
        return tuple(Citation(id=key, observation=text) for key, text in self.used.items())


def _restarts(sig: Signals) -> float:
    """Crash restarts.

    Restarts by the container restart policy are real crashes. Process
    start-time changes also include deliberate restarts (a deploy, or the
    agent's own restart action), so they are only used when the runtime
    could not be asked.
    """
    by_policy = sig.number("container_restarts")
    if by_policy is not None:
        return by_policy
    return sig.number("restarts_2m") or 0.0


def _crashing(sig: Signals, settings: AgentSettings) -> bool:
    """Repeated crashes, or a single crash of a release that was just deployed.

    A release that dies seconds after its deploy is already the answer; waiting
    for the second crash would only delay the rollback (and, at the gateway,
    look like an unexplained error spike in the meantime).
    """
    restarts = _restarts(sig)
    deploy_age = sig.number("seconds_since_last_deploy")
    just_deployed = deploy_age is not None and deploy_age <= settings.deploy_recent_s
    return restarts >= settings.restarts_threshold or (restarts >= 1 and just_deployed)


def _errors_elevated(sig: Signals, settings: AgentSettings) -> bool:
    return (sig.number("err_ratio_1m") or 0.0) >= settings.error_ratio_elevated


def _latency_elevated(sig: Signals, settings: AgentSettings) -> bool:
    return (sig.number("p99_latency_1m") or 0.0) >= settings.latency_p99_elevated_s


def _memory_risk(sig: Signals, settings: AgentSettings) -> bool:
    predicted = sig.number("alert_firing", MEMORY_ALERT) is not None
    rising = (sig.number("rss_slope_1m") or 0.0) > 0
    high = (sig.number("rss_pct_of_limit") or 0.0) >= settings.memory_pct_threshold
    return predicted or (rising and high)


def _saturated(sig: Signals, settings: AgentSettings) -> bool:
    return (sig.number("worker_utilization") or 0.0) >= settings.saturation_utilization


def _down(sig: Signals) -> bool:
    replicas = sig.number("replicas_up")
    running = sig.number("containers_running")
    return replicas == 0 or running == 0


def is_anomalous(sig: Signals, settings: AgentSettings) -> bool:
    """True when the service itself shows a signal the tree can act on."""
    return (
        _crashing(sig, settings)
        or _down(sig)
        or _errors_elevated(sig, settings)
        or _latency_elevated(sig, settings)
        or _memory_risk(sig, settings)
        or _saturated(sig, settings)
    )


def localize(bundle: EvidenceBundle, settings: AgentSettings) -> str:
    """Most downstream anomalous service; the alerting service if none stands out.

    `bundle.services` is ordered nearest-first, so the last anomalous entry is
    the deepest one in the dependency chain.
    """
    anomalous = [s for s in bundle.services if is_anomalous(Signals(s, bundle), settings)]
    return anomalous[-1] if anomalous else bundle.alerting_service


def _result(
    sig: Signals,
    category: Category,
    action: ActionType,
    rationale: str,
    params: dict[str, object] | None = None,
) -> Diagnosis:
    return Diagnosis(
        category=category,
        root_cause_service=sig.service,
        confidence=CONFIDENCE[category],
        insufficient_evidence=category is Category.UNKNOWN,
        evidence=sig.citations(),
        action=ProposedAction(type=action, target_service=sig.service, params=params or {}),
        rationale=rationale,
    )


def diagnose(bundle: EvidenceBundle, settings: AgentSettings) -> Diagnosis:
    """Run the decision tree and return a diagnosis with cited evidence."""
    service = localize(bundle, settings)
    sig = Signals(service, bundle)

    err_ratio = sig.number("err_ratio_1m")
    p99 = sig.number("p99_latency_1m")
    deploy_age = sig.number("seconds_since_last_deploy")
    previous = sig.text("previous_version")
    deployed = sig.text("last_deploy_version")

    if "prometheus" in bundle.incomplete and not _crashing(sig, settings):
        return _result(
            sig,
            Category.UNKNOWN,
            ActionType.ESCALATE,
            "Metrics were unavailable, so there is not enough evidence to diagnose safely.",
        )

    # 2. crash loop
    if _crashing(sig, settings):
        sig.cite(
            "restarts_2m", f"{sig.number('restarts_2m') or 0:.0f} process restarts in 2 minutes"
        )
        sig.cite(
            "container_restarts",
            f"{sig.number('container_restarts') or 0:.0f} restarts by the container restart policy",
        )
        recent = deploy_age is not None and deploy_age <= settings.rollback_lookback_s
        if recent and previous:
            sig.cite("seconds_since_last_deploy", f"deployed {deploy_age:.0f}s ago")
            sig.cite("last_deploy_version", f"running release {deployed}")
            sig.cite("previous_version", f"previous release was {previous}")
            return _result(
                sig,
                Category.CRASH_LOOP,
                ActionType.ROLLBACK,
                f"{service} is restarting repeatedly and release {deployed} was deployed "
                f"{deploy_age:.0f}s ago; rolling back to {previous}.",
                {"to_version": previous},
            )
        return _result(
            sig,
            Category.CRASH_LOOP,
            ActionType.RESTART,
            f"{service} is restarting repeatedly with no recent deploy; restarting once, "
            "then escalating if it continues.",
        )

    errors_up = _errors_elevated(sig, settings)
    latency_up = _latency_elevated(sig, settings)
    if errors_up:
        sig.cite("err_ratio_1m", f"{err_ratio:.1%} of requests failing")
    if latency_up:
        sig.cite("p99_latency_1m", f"p99 latency {p99:.2f}s")

    # 3. bad deploy
    if (
        deploy_age is not None
        and deploy_age <= settings.deploy_recent_s
        and (errors_up or latency_up)
    ):
        sig.cite("seconds_since_last_deploy", f"deployed {deploy_age:.0f}s ago")
        sig.cite("last_deploy_version", f"running release {deployed}")
        sig.cite("previous_version", f"previous release was {previous}")
        sig.cite("restarts_2m", "no crash loop")
        return _result(
            sig,
            Category.BAD_DEPLOY,
            ActionType.ROLLBACK,
            f"Errors or latency rose on {service} right after release {deployed} "
            f"({deploy_age:.0f}s ago) without restarts; rolling back to {previous}.",
            {"to_version": previous} if previous else {},
        )

    # 4. memory leak
    if _memory_risk(sig, settings):
        sig.cite(
            "rss_pct_of_limit", f"memory at {sig.number('rss_pct_of_limit') or 0:.0%} of limit"
        )
        sig.cite("rss_slope_1m", f"growing {(sig.number('rss_slope_1m') or 0) / 1e6:.2f} MB/s")
        sig.cite("alert_firing", "memory exhaustion predicted", MEMORY_ALERT)
        return _result(
            sig,
            Category.MEMORY_LEAK,
            ActionType.RESTART,
            f"{service} memory is rising steadily towards its limit with no recent deploy; "
            "a restart releases the leaked memory.",
        )

    # 5. load saturation
    rate = sig.number("request_rate_1m")
    baseline = sig.number("request_rate_baseline")
    if (
        _saturated(sig, settings)
        and rate is not None
        and baseline is not None
        and baseline > 0
        and rate >= settings.load_surge_factor * baseline
    ):
        sig.cite(
            "worker_utilization", f"{sig.number('worker_utilization') or 0:.0%} of workers busy"
        )
        sig.cite("request_rate_1m", f"{rate:.0f} requests/s now")
        sig.cite("request_rate_baseline", f"{baseline:.0f} requests/s 10 minutes ago")
        current = int(sig.number("containers_running") or sig.number("replicas_up") or 1)
        sig.cite("containers_running", f"{current} replica(s) running")
        return _result(
            sig,
            Category.LOAD_SATURATION,
            ActionType.SCALE,
            f"Load on {service} is {rate / baseline:.1f}x the recent baseline and its workers "
            f"are saturated; adding a replica ({current} -> {current + 1}).",
            {"replicas": current + 1},
        )

    # 6. error spike
    if errors_up:
        sig.cite("seconds_since_last_deploy", "no deploy in the last 10 minutes")
        sig.cite("restarts_2m", "no crash loop")
        sig.cite("request_rate_1m", "load is normal")
        return _result(
            sig,
            Category.ERROR_SPIKE,
            ActionType.RESTART,
            f"{service} is failing {err_ratio:.0%} of requests with no recent deploy, no "
            "restarts and normal load; a restart clears transient bad state.",
        )

    # 7. unknown
    return _result(
        sig,
        Category.UNKNOWN,
        ActionType.ESCALATE,
        "No rule matched the evidence; escalating to a human.",
    )
