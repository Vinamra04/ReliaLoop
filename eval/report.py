"""Turn measured runs into summary.csv, results.json, results.md and charts.

Nothing here invents a value: an unmeasured quantity is `None` in the data
and "n/a" in the tables, a censored time is shown as censored, and every table
states its sample size.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from eval.models import MODES, RunResult

LIMITATIONS = [
    "Small sample: each cell is n runs of one scenario in one mode (n is stated in every "
    "table); with n = 1 there is no variance estimate and no claim of statistical significance.",
    "Single machine: everything, including the load generator, ran on one developer laptop "
    "under Docker Desktop.",
    "Synthetic faults: injected errors, a load surge and a deliberately crashing release; "
    "real incidents are messier.",
    "Compressed time: the demo alerting profile detects in tens of seconds instead of the "
    "minutes to hours of the production profile, and budgets are accounted over 1 hour.",
    "Human approval latency is excluded: rollbacks need approval and the harness approves "
    "them immediately.",
    "The control mode (none) ends after a fixed time, so its time to mitigate is censored "
    "by design unless the fault clears by itself.",
    "LLM results depend on the model and prompt version recorded with the run; a different "
    "model may behave differently.",
]

CSV_FIELDS = [
    "scenario", "mode", "run", "started_at", "error", "detected", "time_to_detect_s",
    "time_to_mitigate_s", "mitigate_censored", "error_budget_consumed_pct",
    "diagnosed_category", "diagnosed_root_cause", "proposed_action", "diagnosis_correct",
    "root_cause_correct", "action_correct", "final_state", "recovery_verified",
    "actions_executed", "wrong_actions_executed", "unsafe_actions_blocked", "approvals_given",
    "degraded_mode", "llm_calls", "llm_tokens_in", "llm_tokens_out", "llm_latency_ms",
    "llm_model", "duration_s",
]  # fmt: skip


def load_runs(path: Path) -> list[RunResult]:
    return [
        RunResult.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _stats(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "mean": round(sum(values) / len(values), 1),
        "min": round(min(values), 1),
        "max": round(max(values), 1),
        "n": len(values),
    }


def _rate(flags: list[bool | None]) -> dict[str, Any] | None:
    known = [flag for flag in flags if flag is not None]
    if not known:
        return None
    return {"rate": round(sum(known) / len(known), 3), "count": sum(known), "n": len(known)}


def aggregate(runs: list[RunResult]) -> dict[str, Any]:
    """Aggregate a group of runs (one cell of a table)."""
    valid = [run for run in runs if run.error is None]
    return {
        "n": len(runs),
        "errors": len(runs) - len(valid),
        "detected": _rate([run.detected for run in valid]),
        "time_to_detect_s": _stats(
            [r.time_to_detect_s for r in valid if r.time_to_detect_s is not None]
        ),
        "time_to_mitigate_s": _stats(
            [r.time_to_mitigate_s for r in valid if r.time_to_mitigate_s is not None]
        ),
        "mitigate_censored": sum(1 for run in valid if run.mitigate_censored),
        "error_budget_consumed_pct": _stats(
            [r.error_budget_consumed_pct for r in valid if r.error_budget_consumed_pct is not None]
        ),
        "diagnosis_accuracy": _rate([run.diagnosis_correct for run in valid]),
        "root_cause_accuracy": _rate([run.root_cause_correct for run in valid]),
        "correct_action_rate": _rate([run.action_correct for run in valid]),
        "recovery_success_rate": _rate([run.recovery_verified for run in valid]),
        "unsafe_actions_blocked": sum(run.unsafe_actions_blocked for run in valid),
        "wrong_actions_executed": sum(run.wrong_actions_executed for run in valid),
        "degraded_runs": sum(1 for run in valid if run.degraded_mode),
        "llm_calls": sum(run.llm_calls for run in valid),
        "llm_tokens_in": sum(run.llm_tokens_in for run in valid),
        "llm_tokens_out": sum(run.llm_tokens_out for run in valid),
        "llm_latency_ms": _stats([r.llm_latency_ms for r in valid if r.llm_latency_ms is not None]),
    }


def build_results(runs: list[RunResult], meta: dict[str, Any]) -> dict[str, Any]:
    """The machine-readable results document."""
    cells: dict[str, dict[str, list[RunResult]]] = defaultdict(lambda: defaultdict(list))
    by_mode: dict[str, list[RunResult]] = defaultdict(list)
    for run in runs:
        cells[run.scenario][run.mode].append(run)
        by_mode[run.mode].append(run)
    return {
        "meta": {**meta, "total_runs": len(runs), "limitations": LIMITATIONS},
        "by_scenario_and_mode": {
            scenario: {mode: aggregate(group) for mode, group in modes.items()}
            for scenario, modes in cells.items()
        },
        "by_mode": {mode: aggregate(group) for mode, group in by_mode.items()},
        "runs": [run.model_dump() for run in runs],
    }


# --- markdown ------------------------------------------------------------------


def _time(stats: dict[str, float] | None, censored: int = 0, n: int = 0) -> str:
    if stats is None:
        return f"censored ({censored}/{n})" if censored else "n/a"
    text = f"{stats['mean']:.0f} s"
    if stats["n"] > 1:
        text += f" ({stats['min']:.0f}-{stats['max']:.0f})"
    if censored:
        text += f"; {censored}/{n} censored"
    return text


def _pct(rate: dict[str, Any] | None) -> str:
    return "n/a" if rate is None else f"{rate['count']}/{rate['n']}"


def _num(stats: dict[str, float] | None, unit: str = "") -> str:
    return "n/a" if stats is None else f"{stats['mean']:.0f}{unit}"


def render_markdown(results: dict[str, Any]) -> str:
    meta = results["meta"]
    lines = [
        "# Evaluation results",
        "",
        f"- **Date:** {meta['date']}",
        f"- **Runs:** {meta['total_runs']} ({meta['runs_per_cell']} per scenario per mode)",
        f"- **Modes:** {', '.join(meta['modes'])}",
        f"- **Scenarios:** {', '.join(meta['scenarios'])}",
        f"- **LLM:** {meta.get('llm_model') or 'not used'} (prompt {meta.get('prompt_version', 'n/a')})",
        f"- **Commit:** {meta.get('commit', 'unknown')}",
        "",
        "Every number below was measured in these runs. `n/a` means not applicable or not "
        "measured; `censored` means the system had not recovered when the run ended.",
        "",
        "## Per scenario and mode",
        "",
        "| Scenario | Mode | n | Detect | Mitigate | Budget used | Category | Root cause | "
        "Action | Recovery verified | Blocked unsafe | Wrong actions |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for scenario in meta["scenarios"]:
        for mode in meta["modes"]:
            cell = results["by_scenario_and_mode"].get(scenario, {}).get(mode)
            if cell is None:
                continue
            lines.append(
                f"| {scenario} | {mode} | {cell['n']} | {_time(cell['time_to_detect_s'])} | "
                f"{_time(cell['time_to_mitigate_s'], cell['mitigate_censored'], cell['n'])} | "
                f"{_num(cell['error_budget_consumed_pct'], '%')} | {_pct(cell['diagnosis_accuracy'])} | "
                f"{_pct(cell['root_cause_accuracy'])} | {_pct(cell['correct_action_rate'])} | "
                f"{_pct(cell['recovery_success_rate'])} | {cell['unsafe_actions_blocked']} | "
                f"{cell['wrong_actions_executed']} |"
            )
    lines += [
        "",
        "Detect = injection to the first page alert firing. Mitigate = injection to the gateway "
        "being back within its objectives and staying there for 30 s. Budget used = failed "
        "gateway requests during the run as a share of the 1-hour error budget. Category, "
        "Root cause and Action are correct/total for the first diagnosis of the run.",
        "",
        "## Per mode",
        "",
        "| Mode | Runs | Detected | Mean detect | Mean mitigate | Censored | Diagnosis accuracy | "
        "Correct action | Recovery verified | Blocked unsafe | Wrong actions | Degraded runs |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for mode in meta["modes"]:
        cell = results["by_mode"].get(mode)
        if cell is None:
            continue
        lines.append(
            f"| {mode} | {cell['n']} | {_pct(cell['detected'])} | {_time(cell['time_to_detect_s'])} | "
            f"{_time(cell['time_to_mitigate_s'])} | {cell['mitigate_censored']}/{cell['n']} | "
            f"{_pct(cell['diagnosis_accuracy'])} | {_pct(cell['correct_action_rate'])} | "
            f"{_pct(cell['recovery_success_rate'])} | {cell['unsafe_actions_blocked']} | "
            f"{cell['wrong_actions_executed']} | {cell['degraded_runs']} |"
        )
    llm = results["by_mode"].get("llm")
    if llm:
        lines += [
            "",
            "## LLM cost (llm mode)",
            "",
            "| Runs | API calls | Tokens in | Tokens out | Mean diagnosis latency | Degraded runs |",
            "|---|---|---|---|---|---|",
            f"| {llm['n']} | {llm['llm_calls']} | {llm['llm_tokens_in']} | {llm['llm_tokens_out']} | "
            f"{_num(llm['llm_latency_ms'], ' ms')} | {llm['degraded_runs']} |",
            "",
            "Latency is per diagnosis and includes evidence-request rounds. A degraded run "
            "is one where the LLM failed and the rule-based engine took over.",
        ]
    failed = [run for run in results["runs"] if run["error"]]
    if failed:
        lines += ["", "## Runs that could not be carried out", ""]
        lines += [f"- {run['scenario']} / {run['mode']}: {run['error']}" for run in failed]
    lines += ["", "## Limitations", ""]
    lines += [f"- {item}" for item in meta["limitations"]]
    lines += [
        "",
        "## Charts",
        "",
        "![Time to mitigate](charts/time_to_mitigate.png)",
        "",
        "![Error budget consumed](charts/error_budget.png)",
        "",
    ]
    return "\n".join(lines)


# --- files ---------------------------------------------------------------------


def write_csv(runs: list[RunResult], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for run in runs:
            row = run.model_dump()
            row["actions_executed"] = ";".join(row["actions_executed"])
            writer.writerow(row)


def write_charts(results: dict[str, Any], directory: Path) -> list[Path]:
    """Two bar charts: time to mitigate and error budget consumed, per scenario and mode."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    directory.mkdir(parents=True, exist_ok=True)
    meta = results["meta"]
    scenarios, modes = meta["scenarios"], meta["modes"]
    colours = {"none": "#8a8a8a", "rules": "#2f7ed8", "llm": "#8e5bd9"}
    written = []
    for key, title, unit, filename in (
        (
            "time_to_mitigate_s",
            "Time to mitigate (lower is better)",
            "seconds",
            "time_to_mitigate.png",
        ),
        (
            "error_budget_consumed_pct",
            "Error budget consumed per run",
            "% of 1 h budget",
            "error_budget.png",
        ),
    ):
        figure, axis = plt.subplots(figsize=(8, 4.2))
        width = 0.8 / max(1, len(modes))
        for offset, mode in enumerate(modes):
            for index, scenario in enumerate(scenarios):
                cell = results["by_scenario_and_mode"].get(scenario, {}).get(mode)
                if cell is None:
                    continue
                x = index + offset * width
                stats = cell[key]
                if stats is not None:
                    bar = axis.bar(x, stats["mean"], width * 0.92, color=colours.get(mode, "#444"))
                    axis.bar_label(bar, fmt="%.0f", fontsize=8)
                elif key == "time_to_mitigate_s" and cell["mitigate_censored"]:
                    axis.text(
                        x,
                        0,
                        "not\nrecovered",
                        ha="center",
                        va="bottom",
                        fontsize=7,
                        color="#b00020",
                    )
        axis.set_xticks([i + width * (len(modes) - 1) / 2 for i in range(len(scenarios))])
        axis.set_xticklabels(scenarios)
        axis.set_ylabel(unit)
        axis.set_title(
            f"{title} (n = {meta['runs_per_cell']} per bar, {meta['date']})", fontsize=10
        )
        handles = [Rectangle((0, 0), 1, 1, color=colours.get(mode, "#444")) for mode in modes]
        axis.legend(handles, modes, title="mode", fontsize=8)
        axis.spines[["top", "right"]].set_visible(False)
        figure.tight_layout()
        path = directory / filename
        figure.savefig(path, dpi=130)
        plt.close(figure)
        written.append(path)
    return written


def write_report(runs: list[RunResult], out_dir: Path, meta: dict[str, Any]) -> dict[str, Any]:
    """Write every output file and return the results document."""
    scenarios = list(dict.fromkeys(run.scenario for run in runs))
    modes = [mode for mode in MODES if any(run.mode == mode for run in runs)]
    per_cell = max(
        (sum(1 for r in runs if (r.scenario, r.mode) == (s, m)) for s in scenarios for m in modes),
        default=0,
    )
    llm_models = sorted({run.llm_model for run in runs if run.llm_model})
    results = build_results(
        runs,
        {
            **meta,
            "scenarios": scenarios,
            "modes": modes,
            "runs_per_cell": per_cell,
            "llm_model": ", ".join(llm_models) or None,
        },
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    write_csv(runs, out_dir / "summary.csv")
    (out_dir / "results.md").write_text(render_markdown(results), encoding="utf-8")
    write_charts(results, out_dir / "charts")
    return results
