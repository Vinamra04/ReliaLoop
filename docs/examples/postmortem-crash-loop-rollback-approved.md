# Postmortem: INC-20261113-045

| | |
|---|---|
| **Service** | gateway |
| **SLO** | gateway-availability |
| **Outcome** | RESOLVED (verified_recovery) |
| **Category** | CRASH_LOOP |
| **Root cause service** | orders |
| **Agent mode** | enforce / diagnosis: rules |
| **Generated** | 2026-11-13 18:06:16 UTC |

This document is blameless: it describes systems, signals and processes, not people.

## Summary

gateway-availability: error budget burning at more than 6x (6m/1m windows). The agent diagnosed **CRASH_LOOP** at **orders** and the incident ended as **RESOLVED**.


## Impact

| Measure | Value |
|---|---|
| Alert started | 2026-11-13 18:04:48 UTC |
| Incident opened | 2026-11-13 18:04:53 UTC |
| Incident ended | 2026-11-13 18:06:16 UTC |
| Duration (alert start to end) | 88 s |
| Failed requests during the incident (gateway) | 226 |
| Total requests during the incident (gateway) | 1760 |
| Error budget consumed by this incident | 550% of the 1h budget of gateway-availability |

Request counts are estimates computed from Prometheus counters over the incident window (5xx responses at the alerting service). The error budget uses the demo profile's 1h accounting window.

## Detection and mitigation

| Step | Time | Since alert start |
|---|---|---|
| Alert started firing | 2026-11-13 18:04:48 UTC | 0 s |
| Incident opened (page received) | 2026-11-13 18:04:53 UTC | 5 s |
| Diagnosed | 2026-11-13 18:04:53 UTC | 5 s |
| Waiting for approval | 2026-11-13 18:04:53 UTC | 5 s |
| Action started | 2026-11-13 18:05:29 UTC | 41 s |
| Verification started | 2026-11-13 18:05:36 UTC | 48 s |
| Recovery verified | 2026-11-13 18:06:16 UTC | 88 s |


## Diagnosis

**1. CRASH_LOOP at orders** — confidence 0.90, engine: rules
orders is restarting repeatedly and release v3-crash was deployed 24s ago; rolling back to v1.

Evidence cited:
- `restarts_2m@orders`: 3 process restarts in 2 minutes- `container_restarts@orders`: 2 restarts by the container restart policy- `seconds_since_last_deploy@orders`: deployed 24s ago- `last_deploy_version@orders`: running release v3-crash- `previous_version@orders`: previous release was v1
Proposed action: `rollback_service` on `orders` {'to_version': 'v1'}
## Actions and approvals

| Action | Target | Risk | Policy decision | Rule | Result | Decided by |
|---|---|---|---|---|---|---|
| `rollback_service` | orders | high | approval_required | high_risk_needs_approval | executed: rolled orders back relialoop/orders:v3-crash -> relialoop/orders:v1: 1 container(s) healthy | operator |


## Verification

- recovery verified: healthy for 3 consecutive checks

## Timeline

| Time (UTC) | State | Event |
|---|---|---|
| 18:04:53 | RECEIVED | incident opened by SLOBurnRateSlow |
| 18:04:53 | ENRICHED | collected 56 evidence items from gateway, orders |
| 18:04:53 | DIAGNOSED | CRASH_LOOP at orders (confidence 0.90, rules) |
| 18:04:53 | VALIDATED | proposal valid: rollback_service on orders |
| 18:04:53 | POLICY_DECIDED | policy: approval_required (high_risk_needs_approval) |
| 18:04:53 | PENDING_APPROVAL | waiting for human approval of rollback_service on orders |
| 18:05:23 | PENDING_APPROVAL | alert SLOBurnRateFast firing |
| 18:05:29 | PENDING_APPROVAL | rollback_service approved by operator |
| 18:05:29 | ACTING | executing rollback_service on orders |
| 18:05:36 | ACTING | rollback_service succeeded: rolled orders back relialoop/orders:v3-crash -> relialoop/orders:v1: 1 container(s) healthy |
| 18:05:36 | VERIFYING | verifying recovery after rollback_service |
| 18:05:53 | VERIFYING | alert SLOBurnRateFast resolved |
| 18:06:16 | RESOLVED | recovery verified: healthy for 3 consecutive checks |
| 18:06:16 | RESOLVED | incident closed: verified_recovery |


## What went well

_To be completed by the reviewer. Prompts: Was the alert timely and actionable? Was the diagnosis right the first time? Did the safety controls (validator, policy, approval) behave as intended?_


## What went badly

_To be completed by the reviewer. Prompts: What delayed detection or mitigation? Which signal was missing or misleading? Was any action unnecessary?_


## Action items

- [ ] _To be completed by the reviewer: one owner and one due date per item; prefer changes to systems and processes over reminders._
