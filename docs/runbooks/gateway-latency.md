# Runbook: gateway-latency

**Alerts:** `SLOBurnRate*` with `slo="gateway-latency"`.

## Summary

Too many gateway API requests take longer than 500 ms. The objective is 95% within 500 ms. Because the budget is 5%, the fast-burn page needs more than 72% of requests to be slow, and the slow-burn page more than 30%.

## Impact

Users experience a slow service; clients with short timeouts see failures. Sustained slowness usually turns into errors (timeouts, shed load).

## Diagnosis

1. Latency percentiles: Service Golden Signals dashboard, or
   `histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{service="gateway", route=~"/api.*"}[1m])))`
2. Is orders slow as well? Same query with `service="orders"`. If yes, the cause is downstream.
3. Saturation: `orders_worker_utilization` near 1 and
   `histogram_quantile(0.95, sum by (le) (rate(orders_queue_wait_seconds_bucket[1m])))` rising mean requests are queueing for worker slots.
4. Load: compare `sum(rate(http_requests_total{service="gateway", route=~"/api.*"}[1m]))` with the value 10 minutes earlier (`offset 10m`).
5. Recent deploy: `service_build_info`. The simulated bad release adds 300 ms to every request.
6. Database: `orders_db_pool_in_use` equal to `orders_db_pool_size` for a long time suggests slow queries.

## Mitigation

**What the agent does:** if load is well above baseline and workers are saturated it scales orders out by one replica (up to the CMDB limit); for a slow release it proposes a rollback, which needs approval.

**Manually:**

- Scale out: `docker compose up -d --scale orders=2 --no-recreate`
- Reduce load during a test: `python -m tools.cli traffic --rps 20`
- Clear an injected latency fault: `python -m tools.cli chaos-clear`

## Escalation

If latency comes from Postgres, escalate to data-team. If scaling is already at `max_replicas`, capacity planning is needed: escalate to platform-team.

## Related

[saturation](saturation.md), [orders-latency](orders-latency.md), [gateway-availability](gateway-availability.md)

Note: an availability page for the same service suppresses the latency page (inhibition rule 3 in `docs/alerting.md`), so during an outage you may only see the availability alert.
