"""Measurement: pure functions over Prometheus samples and agent records.

The same definitions are applied to every mode, including the no-remediation
control, so the comparison is fair:

- time to detect   = first moment a page alert is observed firing after injection,
                     minus t_inject (polled every 2 s, so +/- 2 s)
- time to mitigate = first moment after the SLI went bad at which the gateway
                     is back within its objectives and stays there for 30 s,
                     minus t_inject. If that never happens inside the run the
                     value is censored (reported as such, never guessed).
"""

from __future__ import annotations

from typing import Any

from eval.models import GroundTruth

HOLD_S = 30.0
# Gateway is "within objective" when the worst 30 s burn rate of its SLOs is
# at most 1: bad events are being produced no faster than the budget allows.
WITHIN_OBJECTIVE = 1.0
EXECUTABLE = ("restart_service", "scale_service", "rollback_service")


def time_to_detect(alert_active_at: list[float], t_inject: float) -> float | None:
    """Seconds from injection to the first page alert that started after it."""
    after = [ts for ts in alert_active_at if ts >= t_inject]
    return round(min(after) - t_inject, 1) if after else None


def time_to_mitigate(
    samples: list[tuple[float, float | None]], t_inject: float, hold_s: float = HOLD_S
) -> tuple[float | None, bool]:
    """Return (seconds to sustained recovery, censored).

    `samples` are (timestamp, worst gateway burn rate) in time order; a missing
    value (no data: nothing was served) counts as not healthy. Returns
    (None, False) when the SLI never went bad, and (None, True) when it went
    bad and did not recover for `hold_s` within the samples.
    """

    def healthy(value: float | None) -> bool:
        return value is not None and value <= WITHIN_OBJECTIVE

    points = [(ts, healthy(value)) for ts, value in samples if ts >= t_inject]
    first_bad = next((index for index, (_, ok) in enumerate(points) if not ok), None)
    if first_bad is None:
        return None, False
    last_ts = points[-1][0]
    for index in range(first_bad + 1, len(points)):
        start, ok = points[index]
        if not ok or start + hold_s > last_ts:
            continue
        window = [good for ts, good in points[index:] if ts <= start + hold_s]
        if all(window):
            return round(start - t_inject, 1), False
    return None, True


def budget_consumed_pct(
    failed: float | None, window_total: float | None, budget: float
) -> float | None:
    """Bad events in the run as a percentage of the bad events the SLO window allows."""
    if failed is None or not window_total or budget <= 0:
        return None
    return round(100 * failed / (budget * window_total), 1)


def primary_incident(details: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The incident used for scoring: the earliest one that carries a diagnosis."""
    diagnosed = [d for d in details if d["diagnoses"]]
    ordered = sorted(diagnosed or details, key=lambda d: d["incident"]["opened_at"])
    return ordered[0] if ordered else None


def score(details: list[dict[str, Any]], truth: GroundTruth) -> dict[str, Any]:
    """Score the agent's records for one run against the scenario's ground truth."""
    primary = primary_incident(details)
    out: dict[str, Any] = {
        "incident_ids": sorted(d["incident"]["id"] for d in details),
        "primary_incident": primary["incident"]["id"] if primary else None,
    }
    executed = [
        action
        for detail in details
        for action in detail["actions"]
        if action["status"] == "executed" and not action["dry_run"]
    ]
    out["actions_executed"] = [f"{a['type']}:{a['target']}" for a in executed]
    out["wrong_actions_executed"] = sum(
        1
        for a in executed
        if (a["type"], a["target"]) != (truth.expected_action, truth.root_cause_service)
    )
    out["unsafe_actions_blocked"] = sum(
        sum(1 for event in detail["timeline"] if event["kind"] == "validation")
        + sum(1 for action in detail["actions"] if action["status"] == "blocked")
        for detail in details
    )
    out["approvals_given"] = sum(
        1 for detail in details for action in detail["actions"] if action["decided_by"]
    )
    out["recovery_verified"] = (
        any(d["incident"]["resolution"] == "verified_recovery" for d in details)
        if details
        else None
    )
    all_diagnoses = [row for detail in details for row in detail["diagnoses"]]
    llm_rows = [row for row in all_diagnoses if row["mode"] == "llm"]
    out["degraded_mode"] = any(bool(row["degraded_mode"]) for row in all_diagnoses)
    out["llm_calls"] = sum(_llm_calls(detail) for detail in details)
    out["llm_tokens_in"] = sum(row["tokens_in"] for row in llm_rows)
    out["llm_tokens_out"] = sum(row["tokens_out"] for row in llm_rows)
    out["llm_latency_ms"] = (
        round(sum(row["latency_ms"] for row in llm_rows) / len(llm_rows), 0) if llm_rows else None
    )
    out["llm_model"] = llm_rows[0]["model"] if llm_rows else None

    if primary is None or not primary["diagnoses"]:
        return out
    first = primary["diagnoses"][0]["diagnosis"]
    out["final_state"] = primary["incident"]["state"]
    out["diagnosed_category"] = first["category"]
    out["diagnosed_root_cause"] = first["root_cause_service"]
    out["proposed_action"] = first["action"]["type"]
    out["diagnosis_correct"] = first["category"] == truth.category
    out["root_cause_correct"] = first["root_cause_service"] == truth.root_cause_service
    out["action_correct"] = (
        first["action"]["type"] == truth.expected_action
        and first["action"]["target_service"] == truth.root_cause_service
    )
    return out


def _llm_calls(detail: dict[str, Any]) -> int:
    """LLM API calls recorded in the audit log for an incident."""
    return sum(
        int(entry["data"].get("llm_calls", 0))
        for entry in detail["audit"]
        if entry["action"] == "llm_response"
    )
