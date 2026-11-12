# Runbook: agent-health

**Alerts:** `AgentDown`, `AgentValidationFailuresHigh`, `AgentRecoveryFailing`, `AgentLLMErrorsHigh`, `AgentQueueSaturated` (all ticket, cause).

## Summary

The remediation agent itself is unhealthy. It is monitored like any other production service, and it never remediates itself: these alerts are for humans.

| Alert | Meaning |
|---|---|
| `AgentDown` | No healthy agent has been scraped for 30 s |
| `AgentValidationFailuresHigh` | 3 or more diagnoses rejected by the validator in 10 min |
| `AgentRecoveryFailing` | 2 or more actions in 15 min were followed by failed verification |
| `AgentLLMErrorsHigh` | LLM circuit breaker open, or 3 or more LLM errors in 5 min |
| `AgentQueueSaturated` | Queue depth 50 or more, or webhooks rejected with 429 |

## Impact

No direct user impact. While the agent is down or degraded, incidents are not remediated automatically (or are remediated more slowly, or with the rule-based fallback). Alerts still fire and tickets still exist; humans must act on pages.

## Diagnosis

1. Is it running and ready? `python -m tools.cli ps`, then `curl http://localhost:8080/readyz` (reports `store`, `prometheus` and `docker` separately).
2. Mode and queue: `python -m tools.cli mode`.
3. Recent incidents and why they ended: `python -m tools.cli incidents list`, then `incidents show <id>`. The timeline shows the validator reason or the failed verification check.
4. Logs: `python -m tools.cli logs agent`. Look for `pipeline_error`.
5. Dashboard: Incidents and Agent.

By alert:

- **AgentDown:** container exited or unhealthy. A startup failure is usually an invalid CMDB or policy file (the agent fails fast and logs the reason) or no access to the Docker socket.
- **AgentValidationFailuresHigh:** `sum by (reason) (increase(agent_validation_failures_total[10m]))`. `unknown_evidence_id` means the LLM cited evidence that does not exist; `action_inconsistent_with_category` or `rollback_without_recent_deploy` mean unjustified proposals. If log text contains instructions, suspect prompt injection (see `docs/security.md`).
- **AgentRecoveryFailing:** actions run but do not help: a wrong diagnosis or a fault the agent's actions cannot fix (for example a database outage).
- **AgentLLMErrorsHigh:** `sum by (kind) (increase(agent_llm_errors_total[5m]))`. Rate limits, timeouts or an invalid key. Incidents handled meanwhile are marked `degraded_mode`.
- **AgentQueueSaturated:** an alert storm, or workers stuck in long verifications.

## Mitigation

- Stop the agent from acting while you investigate (the kill switch): `python -m tools.cli mode dry_run` or `mode observe`.
- Restart it: `docker compose restart agent`. Open incidents are re-adopted: verification resumes, approvals keep waiting, interrupted actions are escalated rather than repeated.
- LLM trouble: set `DIAGNOSIS_MODE=rules` in `.env` and run `python -m tools.cli up`.
- Queue saturation: raise `AGENT_WORKERS`, or silence the noisy service during planned work.

## Escalation

platform-team. Until the agent is healthy, treat every page as needing a human.

## Related

[pipeline-health](pipeline-health.md), `docs/agent-design.md`, `docs/operations.md`
