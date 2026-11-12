# Runbook: saturation

**Alert:** `SaturationHigh` (ticket, cause).

## Summary

More than 90% of orders worker slots have been busy for 30 seconds, averaged over replicas. Each replica has 4 slots and handles about 89 requests per second (measured; see `docs/configuration.md`). Beyond that, requests queue for a slot.

## Impact

Queueing raises latency first. A request that waits more than 1.5 s is shed with 503, which the gateway reports as 502. So saturation shows up as a latency burn and then an availability burn.

## Diagnosis

1. Utilization per replica: `orders_worker_utilization`.
2. Queue wait: `histogram_quantile(0.95, sum by (le) (rate(orders_queue_wait_seconds_bucket[1m])))`.
3. Is it a load surge? Compare the request rate now with 10 minutes ago:
   `sum(rate(http_requests_total{service="orders", route=~"/orders.*"}[1m]))` and the same with `offset 10m`.
4. If load is normal but slots are full, each request is taking longer: check for a recent deploy and for slow database calls (`orders_db_pool_in_use`).
5. Replica count: `count(up{service="orders"} == 1)`; the limit is `max_replicas` in the CMDB (3).

## Mitigation

**What the agent does:** when the request rate is at least 1.5 times baseline and utilization is at or above 0.9, it adds one orders replica (never beyond `max_replicas`) and verifies recovery.

**Manually:**

- Scale out: `docker compose up -d --scale orders=2 --no-recreate`
- Reduce test load: `python -m tools.cli traffic --rps 20`

## Escalation

Saturated at `max_replicas`: raise the limit or the per-replica capacity; platform-team decision.

## Related

[orders-latency](orders-latency.md), [gateway-latency](gateway-latency.md), [orders-availability](orders-availability.md)
