# Postmortem: INC-20261113-043

| | |
|---|---|
| **Service** | orders |
| **SLO** | orders-availability |
| **Outcome** | RESOLVED (verified_recovery) |
| **Category** | ERROR_SPIKE |
| **Root cause service** | orders |
| **Agent mode** | enforce / diagnosis: rules |
| **Generated** | 2026-11-13 18:01:40 UTC |

This document is blameless: it describes systems, signals and processes, not people.

## Summary

orders-availability: error budget burning at more than 14.4x (2m/30s windows). The agent diagnosed **ERROR_SPIKE** at **orders** and the incident ended as **RESOLVED**.


## Impact

| Measure | Value |
|---|---|
| Alert started | 2026-11-13 18:00:47 UTC |
| Incident opened | 2026-11-13 18:00:52 UTC |
| Incident ended | 2026-11-13 18:01:40 UTC |
| Duration (alert start to end) | 53 s |
| Failed requests during the incident (orders) | not available |
| Total requests during the incident (orders) | 922 |
| Error budget consumed by this incident | not available |

Request counts are estimates computed from Prometheus counters over the incident window (5xx responses at the alerting service). The error budget uses the demo profile's 1h accounting window.

## Detection and mitigation

| Step | Time | Since alert start |
|---|---|---|
| Alert started firing | 2026-11-13 18:00:47 UTC | 0 s |
| Incident opened (page received) | 2026-11-13 18:00:52 UTC | 5 s |
| Diagnosed | 2026-11-13 18:00:52 UTC | 5 s |
| Action started | 2026-11-13 18:00:52 UTC | 5 s |
| Verification started | 2026-11-13 18:01:00 UTC | 13 s |
| Recovery verified | 2026-11-13 18:01:40 UTC | 53 s |


## Diagnosis

**1. ERROR_SPIKE at orders** — confidence 0.70, engine: rules
orders is failing 16% of requests with no recent deploy, no restarts and normal load; a restart clears transient bad state.

Evidence cited:
- `err_ratio_1m@orders`: 15.7% of requests failing- `seconds_since_last_deploy@orders`: no deploy in the last 10 minutes- `restarts_2m@orders`: no crash loop- `request_rate_1m@orders`: load is normal
Proposed action: `restart_service` on `orders`
## Actions and approvals

| Action | Target | Risk | Policy decision | Rule | Result | Decided by |
|---|---|---|---|---|---|---|
| `restart_service` | orders | low | allow | low_risk_auto | executed: restarted ['relialoop-orders-1']: 1 container(s) healthy | policy (automatic) |


## Verification

- recovery verified: healthy for 3 consecutive checks

## Timeline

| Time (UTC) | State | Event |
|---|---|---|
| 18:00:52 | RECEIVED | incident opened by SLOBurnRateFast |
| 18:00:52 | ENRICHED | collected 28 evidence items from orders |
| 18:00:52 | DIAGNOSED | ERROR_SPIKE at orders (confidence 0.70, rules) |
| 18:00:52 | VALIDATED | proposal valid: restart_service on orders |
| 18:00:52 | POLICY_DECIDED | policy: allow (low_risk_auto) |
| 18:00:52 | ACTING | executing restart_service on orders |
| 18:01:00 | ACTING | restart_service succeeded: restarted ['relialoop-orders-1']: 1 container(s) healthy |
| 18:01:00 | VERIFYING | verifying recovery after restart_service |
| 18:01:22 | VERIFYING | alert SLOBurnRateFast resolved |
| 18:01:22 | VERIFYING | alert SLOBurnRateSlow firing |
| 18:01:40 | RESOLVED | recovery verified: healthy for 3 consecutive checks |
| 18:01:40 | RESOLVED | incident closed: verified_recovery |


## What went well

_To be completed by the reviewer. Prompts: Was the alert timely and actionable? Was the diagnosis right the first time? Did the safety controls (validator, policy, approval) behave as intended?_


## What went badly

_To be completed by the reviewer. Prompts: What delayed detection or mitigation? Which signal was missing or misleading? Was any action unnecessary?_


## Action items

- [ ] _To be completed by the reviewer: one owner and one due date per item; prefer changes to systems and processes over reminders._
