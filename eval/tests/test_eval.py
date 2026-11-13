import csv
import json
from pathlib import Path

import pytest

from eval import measure, report
from eval.models import GroundTruth, RunResult, load_scenarios

T0 = 1000.0
TRUTH = GroundTruth(
    category="ERROR_SPIKE", root_cause_service="orders", expected_action="restart_service"
)


def series(*values: float | None, start: float = T0, step: float = 5.0):
    return [(start + index * step, value) for index, value in enumerate(values)]


# --- scenarios -----------------------------------------------------------------


def test_the_three_scenarios_carry_ground_truth():
    scenarios = load_scenarios()
    assert set(scenarios) == {"error_spike", "latency_saturation", "crash_loop"}
    truths = {
        name: (s.ground_truth.category, s.ground_truth.expected_action)
        for name, s in scenarios.items()
    }
    assert truths == {
        "error_spike": ("ERROR_SPIKE", "restart_service"),
        "latency_saturation": ("LOAD_SATURATION", "scale_service"),
        "crash_loop": ("CRASH_LOOP", "rollback_service"),
    }
    assert all(s.ground_truth.root_cause_service == "orders" for s in scenarios.values())
    assert all(s.max_duration_s > 0 and s.control_duration_s > 0 for s in scenarios.values())


# --- detection -----------------------------------------------------------------


def test_time_to_detect_uses_the_first_alert_after_injection():
    assert measure.time_to_detect([T0 + 14.2, T0 + 31.0], T0) == 14.2
    assert measure.time_to_detect([T0 - 300, T0 + 20], T0) == 20.0  # an older alert is ignored
    assert measure.time_to_detect([], T0) is None
    assert measure.time_to_detect([T0 - 5], T0) is None


# --- mitigation ------------------------------------------------------------------


def test_time_to_mitigate_is_the_start_of_a_sustained_recovery():
    bad, ok = 50.0, 0.0
    samples = series(ok, ok, bad, bad, bad, bad, *[ok] * 10)
    # goes bad at +10 s, healthy again from +30 s and stays so for 30 s
    assert measure.time_to_mitigate(samples, T0) == (30.0, False)


def test_a_short_dip_does_not_count_as_recovery():
    bad, ok = 50.0, 0.0
    samples = series(bad, bad, ok, ok, bad, bad, *[ok] * 9)
    assert measure.time_to_mitigate(samples, T0) == (30.0, False)


def test_never_recovering_is_censored_not_guessed():
    assert measure.time_to_mitigate(series(*[80.0] * 30), T0) == (None, True)


def test_recovery_too_close_to_the_end_of_the_run_is_censored():
    samples = series(50.0, 50.0, 50.0, 0.0, 0.0, 0.0)  # only 10 s of healthy data
    assert measure.time_to_mitigate(samples, T0) == (None, True)


def test_no_impact_at_all_is_neither_a_time_nor_censored():
    assert measure.time_to_mitigate(series(*[0.2] * 20), T0) == (None, False)


def test_missing_data_counts_as_unhealthy():
    samples = series(0.0, None, None, None, *[0.0] * 8)
    assert measure.time_to_mitigate(samples, T0) == (20.0, False)


def test_burn_rate_of_exactly_one_is_within_objective():
    samples = series(5.0, 5.0, *[1.0] * 8)
    assert measure.time_to_mitigate(samples, T0) == (10.0, False)


def test_samples_before_injection_are_ignored():
    samples = series(90.0, 90.0, start=T0 - 10) + series(0.0, 0.0, 0.0)
    assert measure.time_to_mitigate(samples, T0) == (None, False)


def test_budget_consumed():
    # 0.5% of 72,000 requests may fail in the window = 360; 180 failed = 50%
    assert measure.budget_consumed_pct(180, 72_000, 0.005) == 50.0
    assert measure.budget_consumed_pct(0, 72_000, 0.005) == 0.0
    assert measure.budget_consumed_pct(None, 72_000, 0.005) is None
    assert measure.budget_consumed_pct(10, None, 0.005) is None
    assert measure.budget_consumed_pct(10, 0, 0.005) is None


# --- scoring -------------------------------------------------------------------


def detail(
    incident_id: str = "INC-1",
    *,
    opened: str = "2026-11-13T10:00:05+00:00",
    category: str = "ERROR_SPIKE",
    root: str = "orders",
    action: str = "restart_service",
    target: str = "orders",
    mode: str = "rules",
    actions: list[dict] | None = None,
    timeline: list[dict] | None = None,
    resolution: str | None = "verified_recovery",
    diagnosed: bool = True,
    degraded: int = 0,
) -> dict:
    diagnosis = {
        "mode": mode,
        "degraded_mode": degraded,
        "tokens_in": 4000 if mode == "llm" else 0,
        "tokens_out": 300 if mode == "llm" else 0,
        "latency_ms": 8000.0 if mode == "llm" else 0.2,
        "model": "gemini-test" if mode == "llm" else "",
        "diagnosis": {
            "category": category,
            "root_cause_service": root,
            "action": {"type": action, "target_service": target, "params": {}},
        },
    }
    executed = {
        "type": action,
        "target": target,
        "status": "executed",
        "dry_run": 0,
        "decided_by": None,
    }
    return {
        "incident": {
            "id": incident_id,
            "opened_at": opened,
            "state": "RESOLVED",
            "resolution": resolution,
        },
        "diagnoses": [diagnosis] if diagnosed else [],
        "actions": [executed] if actions is None else actions,
        "timeline": timeline or [],
        "audit": [{"action": "llm_response", "data": {"llm_calls": 2}}] if mode == "llm" else [],
    }


def test_a_correct_run_scores_fully():
    scored = measure.score([detail()], TRUTH)
    assert scored["primary_incident"] == "INC-1"
    assert (
        scored["diagnosis_correct"],
        scored["root_cause_correct"],
        scored["action_correct"],
    ) == (True, True, True)
    assert scored["actions_executed"] == ["restart_service:orders"]
    assert scored["wrong_actions_executed"] == 0
    assert scored["recovery_verified"] is True
    assert scored["llm_calls"] == 0


def test_wrong_category_root_cause_and_action_are_each_scored():
    scored = measure.score(
        [
            detail(
                category="BAD_DEPLOY", root="gateway", action="rollback_service", target="gateway"
            )
        ],
        TRUTH,
    )
    assert (
        scored["diagnosis_correct"],
        scored["root_cause_correct"],
        scored["action_correct"],
    ) == (False, False, False)
    assert scored["wrong_actions_executed"] == 1


def test_right_action_on_the_wrong_target_is_not_correct():
    scored = measure.score([detail(target="gateway")], TRUTH)
    assert scored["action_correct"] is False
    assert scored["wrong_actions_executed"] == 1


def test_the_first_diagnosis_of_the_earliest_incident_is_what_counts():
    late = detail("INC-2", opened="2026-11-13T10:00:30+00:00", category="CRASH_LOOP")
    covered = detail("INC-0", opened="2026-11-13T10:00:01+00:00", diagnosed=False, actions=[])
    scored = measure.score([late, detail(), covered], TRUTH)
    assert scored["primary_incident"] == "INC-1"
    assert scored["diagnosis_correct"] is True
    assert scored["incident_ids"] == ["INC-0", "INC-1", "INC-2"]


def test_blocked_and_covered_actions_are_not_executed_actions():
    actions = [
        {
            "type": "rollback_service",
            "target": "gateway",
            "status": "blocked",
            "dry_run": 0,
            "decided_by": None,
        },
        {
            "type": "restart_service",
            "target": "orders",
            "status": "covered",
            "dry_run": 0,
            "decided_by": None,
        },
        {
            "type": "restart_service",
            "target": "orders",
            "status": "dry_run",
            "dry_run": 1,
            "decided_by": None,
        },
    ]
    timeline = [{"kind": "validation"}, {"kind": "state"}, {"kind": "validation"}]
    scored = measure.score([detail(actions=actions, timeline=timeline, resolution=None)], TRUTH)
    assert scored["actions_executed"] == []
    assert scored["unsafe_actions_blocked"] == 3
    assert scored["recovery_verified"] is False


def test_llm_cost_and_approvals_are_summed_over_incidents():
    approved = [
        {
            "type": "rollback_service",
            "target": "orders",
            "status": "executed",
            "dry_run": 0,
            "decided_by": "eval",
        }
    ]
    details = [detail(mode="llm", actions=approved), detail("INC-2", mode="llm", degraded=1)]
    scored = measure.score(details, TRUTH)
    assert (scored["llm_calls"], scored["llm_tokens_in"], scored["llm_tokens_out"]) == (
        4,
        8000,
        600,
    )
    assert scored["llm_latency_ms"] == 8000
    assert scored["llm_model"] == "gemini-test"
    assert scored["approvals_given"] == 1
    assert scored["degraded_mode"] is True


def test_no_incident_means_nothing_to_score():
    scored = measure.score([], TRUTH)
    assert scored["primary_incident"] is None
    assert scored["recovery_verified"] is None
    assert "diagnosis_correct" not in scored


# --- report --------------------------------------------------------------------


def run(scenario: str, mode: str, **fields) -> RunResult:
    base = {
        "scenario": scenario, "mode": mode, "started_at": "2026-11-13T10:00:00+00:00",
        "t_inject": T0, "duration_s": 200.0, "detected": True, "time_to_detect_s": 15.0,
    }  # fmt: skip
    return RunResult.model_validate({**base, **fields})


RUNS = [
    run("error_spike", "none", mitigate_censored=True, error_budget_consumed_pct=400.0),
    run("error_spike", "rules", time_to_mitigate_s=60.0, diagnosis_correct=True, root_cause_correct=True,
        action_correct=True, recovery_verified=True, error_budget_consumed_pct=90.0),
    run("error_spike", "llm", time_to_mitigate_s=75.0, diagnosis_correct=True, root_cause_correct=True,
        action_correct=False, recovery_verified=True, unsafe_actions_blocked=1, llm_calls=2,
        llm_tokens_in=4000, llm_tokens_out=300, llm_latency_ms=8000.0, llm_model="gemini-test"),
    run("crash_loop", "rules", error="EvalError: system did not settle"),
]  # fmt: skip
META = {"date": "2026-11-13", "commit": "abc1234", "prompt_version": "diagnosis-v1"}


def test_aggregate_counts_only_what_was_measured():
    cell = report.aggregate([RUNS[1], RUNS[2]])
    assert cell["n"] == 2
    assert cell["time_to_mitigate_s"] == {"mean": 67.5, "min": 60.0, "max": 75.0, "n": 2}
    assert cell["correct_action_rate"] == {"rate": 0.5, "count": 1, "n": 2}
    assert cell["unsafe_actions_blocked"] == 1

    control = report.aggregate([RUNS[0]])
    assert control["time_to_mitigate_s"] is None
    assert control["mitigate_censored"] == 1
    assert control["diagnosis_accuracy"] is None  # never scored, so not 0%

    failed = report.aggregate([RUNS[3]])
    assert (failed["n"], failed["errors"], failed["detected"]) == (1, 1, None)


def test_report_files_are_written_with_n_date_and_limitations(tmp_path: Path):
    results = report.write_report(RUNS, tmp_path, META)
    assert results["meta"]["runs_per_cell"] == 1
    assert results["meta"]["modes"] == ["none", "rules", "llm"]
    assert results["meta"]["llm_model"] == "gemini-test"

    stored = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert stored["meta"]["total_runs"] == 4
    assert len(stored["meta"]["limitations"]) >= 5
    assert stored["by_scenario_and_mode"]["error_spike"]["llm"]["llm_tokens_in"] == 4000

    with (tmp_path / "summary.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 4
    assert rows[1]["time_to_mitigate_s"] == "60.0"
    assert rows[0]["time_to_mitigate_s"] == ""  # censored stays empty, never a number

    text = (tmp_path / "results.md").read_text(encoding="utf-8")
    assert "**Date:** 2026-11-13" in text
    assert "(1 per scenario per mode)" in text
    assert "| error_spike | none | 1 | 15 s | censored (1/1) | 400% | n/a | n/a | n/a |" in text
    assert "| error_spike | llm | 1 | 15 s | 75 s | n/a | 1/1 | 1/1 | 0/1 | 1/1 | 1 | 0 |" in text
    assert "## Limitations" in text
    assert "Human approval latency is excluded" in text
    assert "crash_loop / rules: EvalError" in text
    assert "| 1 | 2 | 4000 | 300 | 8000 ms | 0 |" in text
    for chart in ("time_to_mitigate.png", "error_budget.png"):
        assert (tmp_path / "charts" / chart).stat().st_size > 2000


def test_runs_round_trip_through_jsonl(tmp_path: Path):
    path = tmp_path / "runs.jsonl"
    path.write_text("\n".join(json.dumps(r.model_dump()) for r in RUNS) + "\n", encoding="utf-8")
    assert report.load_runs(path) == RUNS


@pytest.mark.parametrize(("stats", "censored", "expected"), [
    (None, 0, "n/a"),
    (None, 2, "censored (2/3)"),
    ({"mean": 61.4, "min": 61.4, "max": 61.4, "n": 1}, 0, "61 s"),
    ({"mean": 70.0, "min": 60.0, "max": 80.0, "n": 2}, 1, "70 s (60-80); 1/3 censored"),
])  # fmt: skip
def test_time_formatting(stats, censored, expected):
    assert report._time(stats, censored, 3) == expected
