# Runbook: orders-availability

**Alerts:** `SLOBurnRate*` with `slo="orders-availability"`.

## Summary

The orders service is answering too many requests with 5xx. The objective is 99.5% non-5xx.

## Impact

Every failed orders request becomes a 502 at the gateway, so `gateway-availability` normally fires at the same time. Orders cannot be created or read.

## Diagnosis

1. Status codes: `sum by (status) (rate(http_requests_total{service="orders", route=~"/orders.*"}[1m]))`
   - 500: application errors (bad release, injected fault, database errors).
   - 503: load shedding. A request waited more than 1.5 s for a worker slot. See [saturation](saturation.md).
2. Restarts: `changes(process_start_time_seconds{service="orders"}[5m])`. Two or more means a crash loop.
3. Deploys: `service_build_info{service="orders"}`; a version change just before the errors points at the release.
4. Database: `/readyz` on orders reports Postgres connectivity; check `orders_db_pool_in_use`.
5. Logs: `python -m tools.cli logs orders`. Look at the most frequent error message.
6. Is it one replica or all? Break the first query down `by (instance, status)`.

## Mitigation

**What the agent does:** restarts orders for an error spike with no deploy and no restarts; scales out when shedding is caused by a load surge; proposes a rollback (approval required) when errors started right after a deploy.

**Manually:**

- Restart: `docker compose restart orders`
- Roll back: `python -m tools.cli deploy --service orders --version v1`
- Clear injected faults: `python -m tools.cli chaos-clear`

## Escalation

Database errors: data-team. Repeated failures after a restart and a rollback: platform-team; the agent escalates automatically after one failed re-plan.

## Related

[gateway-availability](gateway-availability.md), [crash-loop](crash-loop.md), [saturation](saturation.md), [orders-latency](orders-latency.md)
