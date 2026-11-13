# Evaluation results

All numbers here were measured on 2026-11-13 on one laptop (Windows 11, Docker
Desktop). Nothing is estimated or invented. Each folder holds the raw
per-run records (`runs.jsonl`) and, for the two full attempts, the generated
report (`results.md`, `results.json`, `summary.csv`, `charts/`).

| Folder | What it is | Runs |
|---|---|---|
| [2026-11-13-run1](2026-11-13-run1/results.md) | Full attempt 1: 3 scenarios x 3 modes, commit `1a6181a`, saturation surge 150 rps | 9 (n = 1 per cell) |
| [2026-11-13-run2](2026-11-13-run2/results.md) | Full attempt 2: 3 scenarios x 3 modes, commit `2484265`, saturation surge 130 rps | 9 (n = 1 per cell) |
| [2026-11-13-control-clean-baseline](2026-11-13-control-clean-baseline/runs.jsonl) | Control runs only (mode `none`), commit `82964b4`, after the baseline fix described below | 3 |

## Read this before quoting a detection time

**Detection times in run1 and run2 are optimistic.** In those runs the harness
injected the next fault before the burn-rate windows of the previous run had
emptied (the slow-burn alert looks back 6 minutes). The alert rules therefore
started each run already "warm" and fired after 6 to 17 seconds. Because the
agent starts working when the alert fires, **time to mitigate in run1 and run2
carries the same bias**.

The harness was then fixed to wait until the whole 6-minute window is clean
and to record detection when a page alert is actually seen firing. Three
control runs (no remediation) were completed with that fix:

| Scenario | Mode | Detect (clean baseline) | Recovered by itself | Error budget used |
|---|---|---|---|---|
| crash_loop | none | 46.5 s | no (censored) | 100% |
| error_spike | none | 24.1 s | no (censored) | 240% |
| latency_saturation | none | 16.1 s | no (censored) | 1093% |

Treat these as the honest detection times: roughly 15 to 50 seconds depending
on the fault. A manual error-spike run on the same day
([docs/DEMO.md](../DEMO.md)) opened its incident 20 s after injection.

A later attempt to repeat the `rules` and `llm` runs from a clean baseline
failed with connection errors (the stack went down mid-run) and produced no
measurements, so it is not included.

## What is valid in run1 and run2

These do not depend on when the alert fired:

- **Diagnosis** (category and root-cause service): correct in 6/6 `rules` runs and 6/6 `llm` runs.
- **Action**: the expected action was proposed and executed in 6/6 `rules` runs and 6/6 `llm` runs; no wrong action was executed.
- **Recovery**: verified by the agent in 12/12 remediated runs. In one `llm` run (run1, latency_saturation) the agent verified recovery but the gateway SLI had not stayed within objective for 30 s when the run ended, so its time to mitigate is censored.
- **Control**: in 6/6 runs without remediation the system had not recovered when the run ended.

## Limitations

- Small sample: n = 1 per cell per attempt, n = 2 when both attempts are read together. No variance estimate, no statistical claim.
- The two attempts are not identical: the saturation surge was 150 rps in run1 and 130 rps in run2.
- Single machine, synthetic faults, and a demo alerting profile that compresses hours into minutes (budgets are accounted over 1 hour).
- Human approval latency is excluded: the harness approves rollbacks immediately.
- LLM runs used `gemini-3.5-flash-lite` with prompt `diagnosis-v1`; another model may behave differently.
