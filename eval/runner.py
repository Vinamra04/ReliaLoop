"""Evaluation runner.

For each scenario and mode: reset the system to a healthy baseline, wait until
it is quiet, inject the fault, let the agent (or nobody, in the control mode)
deal with it, measure, and always clean up. Runs are serial and every wait is
bounded, so one bad run cannot hang or poison the next.

Modes:
- none:  control. The agent only observes; nothing is remediated. Shows what
         the fault costs when left alone for `control_duration_s`.
- rules: the agent acts, diagnosing with the deterministic rule engine.
- llm:   the agent acts, diagnosing with the LLM (same validator and policy).

Rollbacks need human approval. The runner approves them immediately, so
**human approval latency is not part of the measured times**.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from eval import measure
from eval.models import Mode, RunResult, Scenario
from libs.core.gate import GateDecision
from tools import paths, stack
from tools.deploy.run import EventRecorder, current_version, deploy, write_version

BASE_RPS = 20.0
POLL_S = 2.0
SETTLE_S = 30.0
MAX_SETTLE_S = 900.0
OBSERVE_AFTER_S = 45.0
BASELINE_LOOKBACK_FACTOR = 1.5
STEP_S = 5
API = 'route!~"/healthz|/readyz|/metrics|/chaos.*"'
GATEWAY_BURN = 'max(slo:burn_rate:ratio_30s{service="gateway"})'
TERMINAL = {"RESOLVED", "ESCALATED", "REJECTED", "DRY_RUN_COMPLETE", "OBSERVED"}
AGENT_MODES: dict[Mode, tuple[str, str]] = {
    "none": ("observe", "rules"),
    "rules": ("enforce", "rules"),
    "llm": ("enforce", "llm"),
}
EVAL_GATE = GateDecision(True, 0, "OVERRIDE: evaluation fault injection", overridden=True)

Log = Callable[[str], None]


class EvalError(RuntimeError):
    """The run could not be carried out (not a property of the system under test)."""


class Stack:
    """Thin client over the running system: Prometheus, the agent, chaos and load."""

    def __init__(self, env: dict[str, str], client: httpx.Client) -> None:
        self.env = env
        self.http = client
        self.prom = stack.local_url("PROMETHEUS_PORT", env)
        self.agent = stack.local_url("AGENT_PORT", env)
        self.gateway = stack.local_url("GATEWAY_PORT", env)
        self.loadgen = stack.local_url("LOADGEN_PORT", env)
        self._approver = {"X-Approver-Token": env["APPROVER_TOKEN"]}
        self._chaos = {"X-Chaos-Token": env["CHAOS_TOKEN"]}

    # --- Prometheus -------------------------------------------------------------

    def query(self, expr: str) -> float | None:
        response = self.http.get(f"{self.prom}/api/v1/query", params={"query": expr})
        response.raise_for_status()
        result = response.json()["data"]["result"]
        if not result:
            return None
        value = float(result[0]["value"][1])
        return value if value == value else None  # NaN -> None

    def query_range(self, expr: str, start: float, end: float) -> list[tuple[float, float | None]]:
        """Samples every `STEP_S` seconds; a step without data is (timestamp, None)."""
        response = self.http.get(
            f"{self.prom}/api/v1/query_range",
            params={"query": expr, "start": start, "end": end, "step": STEP_S},
        )
        response.raise_for_status()
        result = response.json()["data"]["result"]
        found = {float(ts): float(value) for ts, value in result[0]["values"]} if result else {}
        steps = int((end - start) // STEP_S) + 1
        out: list[tuple[float, float | None]] = []
        for index in range(steps):
            ts = start + index * STEP_S
            value = found.get(ts)
            out.append((ts, value if value is not None and value == value else None))
        return out

    def page_alerts(self) -> dict[str, float]:
        """Firing page alerts: `name/slo-or-service` -> activeAt (unix seconds)."""
        response = self.http.get(f"{self.prom}/api/v1/alerts")
        response.raise_for_status()
        firing: dict[str, float] = {}
        for alert in response.json()["data"]["alerts"]:
            labels = alert["labels"]
            if alert["state"] == "firing" and labels.get("severity") == "page":
                key = f"{labels['alertname']}/{labels.get('slo') or labels.get('service', '')}"
                active = datetime.fromisoformat(alert["activeAt"].replace("Z", "+00:00"))
                firing[key] = active.timestamp()
        return firing

    # --- agent ------------------------------------------------------------------

    def set_mode(self, mode: Mode) -> None:
        agent_mode, diagnosis = AGENT_MODES[mode]
        response = self.http.post(
            f"{self.agent}/admin/mode",
            json={"mode": agent_mode, "diagnosis_mode": diagnosis, "actor": "eval"},
            headers=self._approver,
        )
        response.raise_for_status()

    def incidents_since(self, t0: float) -> list[dict[str, Any]]:
        """Full records of incidents opened at or after `t0`."""
        response = self.http.get(f"{self.agent}/incidents", params={"limit": 100})
        response.raise_for_status()
        details = []
        for incident in response.json()["incidents"]:
            if datetime.fromisoformat(incident["opened_at"]).timestamp() >= t0 - 1:
                detail = self.http.get(f"{self.agent}/incidents/{incident['id']}")
                detail.raise_for_status()
                details.append(detail.json())
        return details

    def approve_pending(self, incident_ids: set[str]) -> int:
        """Approve pending actions of this run's incidents; return how many."""
        response = self.http.get(f"{self.agent}/actions/pending")
        response.raise_for_status()
        approved = 0
        for action in response.json()["actions"]:
            if action["incident_id"] in incident_ids:
                result = self.http.post(
                    f"{self.agent}/incidents/{action['incident_id']}/approve",
                    json={"actor": "eval-auto-approve"},
                    headers=self._approver,
                )
                approved += result.status_code == 200
        return approved

    # --- faults and load ----------------------------------------------------------

    def clear_chaos(self) -> None:
        for path in ("/chaos", "/chaos/target/orders"):
            self.http.delete(f"{self.gateway}{path}", headers=self._chaos)

    def chaos_errors(self, rate: float, duration_s: int) -> None:
        response = self.http.post(
            f"{self.gateway}/chaos/target/orders/errors",
            json={"rate": rate, "duration_s": duration_s},
            headers=self._chaos,
        )
        response.raise_for_status()

    def traffic(self, rps: float, duration_s: float | None = None) -> None:
        body: dict[str, Any] = {"rps": rps}
        if duration_s is not None:
            body["duration_s"] = duration_s
        response = self.http.post(
            f"{self.loadgen}/rate",
            json=body,
            headers={"X-Loadgen-Token": self.env["LOADGEN_TOKEN"]},
        )
        response.raise_for_status()

    def recorder(self) -> EventRecorder:
        return EventRecorder(
            self.http,
            self.agent,
            self.env["WEBHOOK_TOKEN"],
            paths.REPO_ROOT / "data" / "deploy-events.log",
        )


def _compose_up_orders(wait_timeout_s: int = 60) -> bool:
    code = stack.compose(
        "up",
        "-d",
        "--no-deps",
        "--wait",
        "--wait-timeout",
        str(wait_timeout_s),
        "--scale",
        "orders=1",
        "orders",
        check=False,
    )
    return code == 0


def inject(scenario: Scenario, system: Stack) -> None:
    """Start the fault described by the scenario."""
    fault = scenario.inject
    if fault.kind == "chaos_errors":
        system.chaos_errors(fault.rate, fault.duration_s)
    elif fault.kind == "traffic_surge":
        system.traffic(fault.rps, fault.duration_s)
    else:
        result = deploy(
            service=fault.service,
            version=fault.version,
            gate=EVAL_GATE,
            versions_file=paths.VERSIONS_FILE,
            recorder=system.recorder(),
            # A crashing release never becomes healthy: do not wait long for it.
            compose_up=lambda _service: _compose_up_orders(20),
            image_exists=stack.image_exists,
            actor="eval",
        )
        if result.previous_version is None and not result.ok:
            raise EvalError(f"could not deploy {fault.version}: {result.message}")


def reset_baseline(system: Stack, log: Log) -> None:
    """Good versions, no faults, one replica, baseline load, agent only observing."""
    system.set_mode("none")
    system.clear_chaos()
    system.traffic(BASE_RPS)
    running = current_version(paths.VERSIONS_FILE, "orders")
    if running != "v1":
        log(f"  cleanup: restoring orders {running} -> v1")
        write_version(paths.VERSIONS_FILE, "orders", "v1")
        # Recorded as a rollback so the reverted release is not blamed for later faults.
        system.recorder().record(
            {
                "service": "orders",
                "version": "v1",
                "previous_version": running,
                "status": "completed",
                "reason": "rollback",
                "actor": "eval-cleanup",
            }
        )
    if not _compose_up_orders():
        raise EvalError("orders did not become healthy during cleanup")
    system.clear_chaos()


def settle(system: Stack, log: Log, max_settle_s: float = MAX_SETTLE_S) -> float:
    """Wait until the system has been quiet for `SETTLE_S`; return how long that took.

    Quiet means: no page alert firing, the gateway within its objectives, and the
    request rate of 10 minutes ago back at baseline (the agent compares load with
    that moment, so an earlier surge must have left its look-back).
    """
    started = time.time()
    quiet_since: float | None = None
    while time.time() - started < max_settle_s:
        burn = system.query(GATEWAY_BURN)
        lookback = system.query(
            f'sum(rate(http_requests_total{{service="orders", {API}}}[1m] offset 10m))'
        )
        quiet = (
            not system.page_alerts()
            and burn is not None
            and burn <= measure.WITHIN_OBJECTIVE
            and (lookback is None or lookback < BASELINE_LOOKBACK_FACTOR * BASE_RPS)
        )
        now = time.time()
        quiet_since = (quiet_since or now) if quiet else None
        if quiet_since is not None and now - quiet_since >= SETTLE_S:
            return round(now - started, 1)
        time.sleep(POLL_S)
    raise EvalError(f"system did not settle within {max_settle_s:.0f}s")


def run_one(scenario: Scenario, mode: Mode, system: Stack, log: Log, run: int = 1) -> RunResult:
    """Carry out one run. Always cleans up the fault, whatever happens."""
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    wall = time.time()
    t_inject = wall
    alert_times: dict[str, float] = {}
    approvals = 0
    error: str | None = None
    details: list[dict[str, Any]] = []
    try:
        reset_baseline(system, log)
        waited = settle(system, log)
        log(f"  baseline quiet after {waited:.0f}s; injecting")
        system.set_mode(mode)
        t_inject = time.time()
        inject(scenario, system)

        limit = scenario.control_duration_s if mode == "none" else scenario.max_duration_s
        finished_at: float | None = None
        while time.time() - t_inject < limit + OBSERVE_AFTER_S:
            for key, active in system.page_alerts().items():
                if active >= t_inject - 1:
                    alert_times.setdefault(key, active)
            if mode != "none":
                details = system.incidents_since(t_inject)
                approvals += system.approve_pending({d["incident"]["id"] for d in details})
                done = bool(details) and all(d["incident"]["state"] in TERMINAL for d in details)
                if done and finished_at is None:
                    finished_at = time.time()
                    log(f"  agent finished after {finished_at - t_inject:.0f}s; observing")
                # Keep watching long enough to see whether the recovery holds.
                if finished_at is not None and time.time() - finished_at >= OBSERVE_AFTER_S:
                    break
                if finished_at is None and time.time() - t_inject >= limit:
                    break
            elif time.time() - t_inject >= limit:
                break
            time.sleep(POLL_S)
        details = system.incidents_since(t_inject)
    except (EvalError, httpx.HTTPError) as exc:
        error = f"{type(exc).__name__}: {exc}"[:300]
        log(f"  run error: {error}")

    t_end = time.time()
    result = RunResult(
        scenario=scenario.name,
        mode=mode,
        run=run,
        started_at=started_at,
        t_inject=round(t_inject, 1),
        duration_s=round(t_end - wall, 1),
        error=error,
    )
    try:
        if error is None:
            _measure(result, scenario, mode, system, t_inject, t_end, alert_times, details)
            result.approvals_given = approvals
    except httpx.HTTPError as exc:
        result.error = f"measurement failed: {type(exc).__name__}"
    finally:
        try:
            system.set_mode("none")
            system.clear_chaos()
            system.traffic(BASE_RPS)
        except httpx.HTTPError:
            log("  warning: cleanup request failed")
    return result


def _measure(
    result: RunResult,
    scenario: Scenario,
    mode: Mode,
    system: Stack,
    t_inject: float,
    t_end: float,
    alert_times: dict[str, float],
    details: list[dict[str, Any]],
) -> None:
    result.time_to_detect_s = measure.time_to_detect(list(alert_times.values()), t_inject)
    result.detected = result.time_to_detect_s is not None
    start = t_inject - (t_inject % STEP_S)
    samples = system.query_range(GATEWAY_BURN, start, t_end)
    result.time_to_mitigate_s, result.mitigate_censored = measure.time_to_mitigate(
        samples, t_inject
    )

    window = max(1, int(t_end - t_inject))
    selector = f'http_requests_total{{service="gateway", {API}'
    failed = system.query(f'sum(increase({selector}, status=~"5.."}}[{window}s]))')
    total_hour = system.query(f"sum(increase({selector}}}[1h]))")
    objective = system.query('slo:objective:ratio{slo="gateway-availability"}')
    if objective is not None:
        result.error_budget_consumed_pct = measure.budget_consumed_pct(
            failed or 0.0, total_hour, 1 - objective
        )

    scored = measure.score(details, scenario.ground_truth)
    if mode == "none":
        # The control only observes: there is no agent decision to score.
        for key in ("incident_ids", "primary_incident"):
            setattr(result, key, scored[key])
        return
    for key, value in scored.items():
        setattr(result, key, value)


def run_all(
    scenarios: list[Scenario],
    modes: list[Mode],
    out_dir: Path,
    log: Log,
    runs: int = 1,
) -> list[RunResult]:
    """Run every (mode, scenario) pair serially and append each result to runs.jsonl."""
    env = stack.load_env()
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[RunResult] = []
    total = len(scenarios) * len(modes) * runs
    with httpx.Client(timeout=15.0) as client:
        system = Stack(env, client)
        try:
            for mode in modes:
                for scenario in scenarios:
                    for run in range(1, runs + 1):
                        log(f"[{len(results) + 1}/{total}] {scenario.name} / {mode}")
                        result = run_one(scenario, mode, system, log, run)
                        results.append(result)
                        with (out_dir / "runs.jsonl").open(
                            "a", encoding="utf-8", newline="\n"
                        ) as f:
                            f.write(json.dumps(result.model_dump()) + "\n")
                        log(
                            f"  detect={result.time_to_detect_s}s "
                            f"mitigate={result.time_to_mitigate_s}s "
                            f"censored={result.mitigate_censored} "
                            f"category={result.diagnosed_category} "
                            f"state={result.final_state} actions={result.actions_executed}"
                        )
        finally:
            log("final cleanup")
            try:
                reset_baseline(system, log)
            except (EvalError, httpx.HTTPError) as exc:
                log(f"warning: final cleanup failed: {exc}")
    return results
