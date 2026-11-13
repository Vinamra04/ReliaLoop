# Evaluation results

- **Date:** 2026-11-13
- **Runs:** 9 (1 per scenario per mode)
- **Modes:** none, rules, llm
- **Scenarios:** crash_loop, error_spike, latency_saturation
- **LLM:** gemini-3.5-flash-lite (prompt diagnosis-v1)
- **Commit:** 1a6181a

Every number below was measured in these runs. `n/a` means not applicable or not measured; `censored` means the system had not recovered when the run ended.

## Per scenario and mode

| Scenario | Mode | n | Detect | Mitigate | Budget used | Category | Root cause | Action | Recovery verified | Blocked unsafe | Wrong actions |
|---|---|---|---|---|---|---|---|---|---|---|---|
| crash_loop | none | 1 | 6 s | censored (1/1) | 163% | n/a | n/a | n/a | n/a | 0 | 0 |
| crash_loop | rules | 1 | 16 s | 73 s | 46% | 1/1 | 1/1 | 1/1 | 1/1 | 0 | 0 |
| crash_loop | llm | 1 | 6 s | 62 s | 38% | 1/1 | 1/1 | 1/1 | 1/1 | 0 | 0 |
| error_spike | none | 1 | 10 s | censored (1/1) | 409% | n/a | n/a | n/a | n/a | 0 | 0 |
| error_spike | rules | 1 | 11 s | 52 s | 51% | 1/1 | 1/1 | 1/1 | 1/1 | 0 | 0 |
| error_spike | llm | 1 | 16 s | 62 s | 52% | 1/1 | 1/1 | 1/1 | 1/1 | 0 | 0 |
| latency_saturation | none | 1 | 6 s | censored (1/1) | 2225% | n/a | n/a | n/a | n/a | 0 | 0 |
| latency_saturation | rules | 1 | 10 s | 56 s | 269% | 1/1 | 1/1 | 1/1 | 1/1 | 0 | 0 |
| latency_saturation | llm | 1 | 7 s | censored (1/1) | 234% | 1/1 | 1/1 | 1/1 | 1/1 | 0 | 0 |

Detect = injection to the first page alert firing. Mitigate = injection to the gateway being back within its objectives and staying there for 30 s. Budget used = failed gateway requests during the run as a share of the 1-hour error budget. Category, Root cause and Action are correct/total for the first diagnosis of the run.

## Per mode

| Mode | Runs | Detected | Mean detect | Mean mitigate | Censored | Diagnosis accuracy | Correct action | Recovery verified | Blocked unsafe | Wrong actions | Degraded runs |
|---|---|---|---|---|---|---|---|---|---|---|---|
| none | 3 | 3/3 | 8 s (6-10) | n/a | 3/3 | n/a | n/a | n/a | 0 | 0 | 0 |
| rules | 3 | 3/3 | 12 s (10-16) | 60 s (52-73) | 0/3 | 3/3 | 3/3 | 3/3 | 0 | 0 | 0 |
| llm | 3 | 3/3 | 10 s (6-16) | 62 s (62-62) | 1/3 | 3/3 | 3/3 | 3/3 | 0 | 0 | 0 |

## LLM cost (llm mode)

| Runs | API calls | Tokens in | Tokens out | Mean diagnosis latency | Degraded runs |
|---|---|---|---|---|---|
| 3 | 3 | 11809 | 824 | 2199 ms | 0 |

Latency is per diagnosis and includes evidence-request rounds. A degraded run is one where the LLM failed and the rule-based engine took over.

## Limitations

- Small sample: each cell is n runs of one scenario in one mode (n is stated in every table); with n = 1 there is no variance estimate and no claim of statistical significance.
- Single machine: everything, including the load generator, ran on one developer laptop under Docker Desktop.
- Synthetic faults: injected errors, a load surge and a deliberately crashing release; real incidents are messier.
- Compressed time: the demo alerting profile detects in tens of seconds instead of the minutes to hours of the production profile, and budgets are accounted over 1 hour.
- Human approval latency is excluded: rollbacks need approval and the harness approves them immediately.
- The control mode (none) ends after a fixed time, so its time to mitigate is censored by design unless the fault clears by itself.
- LLM results depend on the model and prompt version recorded with the run; a different model may behave differently.

## Charts

![Time to mitigate](charts/time_to_mitigate.png)

![Error budget consumed](charts/error_budget.png)
