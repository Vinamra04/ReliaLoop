# Runbook: pipeline-health

**Alerts:** `Watchdog` (heartbeat), `PrometheusTargetDown`, `AlertmanagerNotificationsFailing` (ticket, cause).

## Summary

These alerts are about the monitoring and alerting pipeline itself.

- `Watchdog` always fires. It is routed to a null receiver. Its *absence* from Alertmanager means rule evaluation or alert delivery is broken.
- `PrometheusTargetDown`: Prometheus cannot scrape prometheus, alertmanager, loadgen or the agent.
- `AlertmanagerNotificationsFailing`: Alertmanager could not deliver notifications, so pages may not reach the agent.

## Impact

No direct user impact, but the system is partly blind: a real incident may not be detected or may not reach the agent.

## Diagnosis

Run the end-to-end check first:

```
python -m tools.cli verify-pipeline
```

It checks targets, rule health, the Watchdog, webhook authentication and delivers a synthetic test alert through Alertmanager to the agent. The first failing line tells you which hop is broken.

Then, depending on the failure:

1. Targets: `http://localhost:9090/targets` shows the scrape error per target.
2. Rules not loading: `python -m tools.cli slo validate` runs `promtool check rules` and `promtool check config`.
3. Notifications failing: `python -m tools.cli logs alertmanager`.
   - `401`: the token in `.secrets/webhook_token` differs from `WEBHOOK_TOKEN` in `.env`. Both are written by `python -m tools.cli init`.
   - connection refused: the agent is down; `python -m tools.cli ps`.
4. Dashboard: Pipeline Health.

## Mitigation

- Restart the failing component: `docker compose restart alertmanager` (or `prometheus`, `agent`).
- Reload configuration after an edit: `python -m tools.cli up` reloads Prometheus and Alertmanager.
- Token mismatch: run `python -m tools.cli init`, then `python -m tools.cli up`.

The agent does not remediate pipeline components: they are not managed services.

## Escalation

platform-team. While the pipeline is degraded, watch the Golden Signals dashboard manually.

## Related

`docs/alerting.md`, `docs/operations.md`
