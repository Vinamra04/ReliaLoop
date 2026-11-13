# Postmortem: INC-20261113-035

| | |
|---|---|
| **Service** | orders |
| **SLO** | orders-availability |
| **Outcome** | RESOLVED (verified_recovery) |
| **Category** | LOAD_SATURATION |
| **Root cause service** | orders |
| **Agent mode** | enforce / diagnosis: llm |
| **Generated** | 2026-11-13 08:17:25 UTC |

This document is blameless: it describes systems, signals and processes, not people.

## Summary

> **AI-drafted — human review required.**

An error budget burn on the orders-availability SLO was triggered by load saturation in the orders service. The automated remediation agent diagnosed the issue and scaled the orders service from 1 to 2 containers. Recovery was successfully verified and the incident was closed within 78 seconds.


## Impact

| Measure | Value |
|---|---|
| Alert started | 2026-11-13 08:16:07 UTC |
| Incident opened | 2026-11-13 08:16:15 UTC |
| Incident ended | 2026-11-13 08:17:25 UTC |
| Duration (alert start to end) | 78 s |
| Failed requests during the incident (orders) | 674 |
| Total requests during the incident (orders) | 9915 |
| Error budget consumed by this incident | 96% of the 1h budget of orders-availability |

Request counts are estimates computed from Prometheus counters over the incident window (5xx responses at the alerting service). The error budget uses the demo profile's 1h accounting window.

## Detection and mitigation

| Step | Time | Since alert start |
|---|---|---|
| Alert started firing | 2026-11-13 08:16:07 UTC | 0 s |
| Incident opened (page received) | 2026-11-13 08:16:15 UTC | 8 s |
| Diagnosed | 2026-11-13 08:16:18 UTC | 11 s |
| Action started | 2026-11-13 08:16:18 UTC | 11 s |
| Verification started | 2026-11-13 08:16:24 UTC | 18 s |
| Recovery verified | 2026-11-13 08:17:25 UTC | 78 s |


## Diagnosis

**1. LOAD_SATURATION at orders** — confidence 0.95, engine: llm (gemini gemini-3.5-flash-lite, prompt diagnosis-v1, 2971 tokens in / 265 out)
The request rate is well above the baseline and worker utilization is at 0.978, leading to errors from shed load and elevated latency due to saturation. Scaling the service to add a replica is justified.

Evidence cited:
- `request_rate_1m@orders`: Request rate is significantly above baseline.- `request_rate_baseline@orders`: Baseline request rate is much lower than current.- `worker_utilization@orders`: Worker utilization is near 1.0.
Proposed action: `scale_service` on `orders` {'replicas': 2}
## Actions and approvals

| Action | Target | Risk | Policy decision | Rule | Result | Decided by |
|---|---|---|---|---|---|---|
| `scale_service` | orders | low | allow | low_risk_auto | executed: scaled orders 1 -> 2, created ['relialoop-orders-2']: 2 container(s) healthy | policy (automatic) |


## Verification

- recovery verified: healthy for 3 consecutive checks

## Timeline

| Time (UTC) | State | Event |
|---|---|---|
| 08:16:15 | RECEIVED | incident opened by SLOBurnRateFast |
| 08:16:15 | ENRICHED | collected 27 evidence items from orders |
| 08:16:18 | DIAGNOSED | LOAD_SATURATION at orders (confidence 0.95, llm) |
| 08:16:18 | VALIDATED | proposal valid: scale_service on orders |
| 08:16:18 | POLICY_DECIDED | policy: allow (low_risk_auto) |
| 08:16:18 | ACTING | executing scale_service on orders |
| 08:16:24 | ACTING | scale_service succeeded: scaled orders 1 -> 2, created ['relialoop-orders-2']: 2 container(s) healthy |
| 08:16:24 | VERIFYING | verifying recovery after scale_service |
| 08:16:45 | VERIFYING | alert SLOBurnRateSlow firing |
| 08:17:15 | VERIFYING | alert SLOBurnRateFast resolved |
| 08:17:25 | RESOLVED | recovery verified: healthy for 3 consecutive checks |
| 08:17:25 | RESOLVED | incident closed: verified_recovery |


## What went well

> **AI-drafted — human review required.**

- Detection occurred rapidly, with the alert starting to fire at 08:16:07 UTC and the incident opening 8 seconds later.
- The diagnosis mode identified load saturation in the orders service with a confidence of 0.95 in 11 seconds.
- Automated remediation successfully scaled the orders service from 1 to 2 containers, achieving healthy status across 2 containers.
- Recovery verification successfully passed 3 consecutive checks and resolved the incident.


## What went badly

> **AI-drafted — human review required.**

- _(nothing drafted)_


## Action items

> **AI-drafted — human review required.**

- [ ] _(nothing drafted)_
