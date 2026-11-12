# Runbook: orders-latency

**Alerts:** `SLOBurnRate*` with `slo="orders-latency"`.

## Summary

Too many orders requests take longer than 300 ms. The objective is 95% within 300 ms. A healthy request takes about 50 ms (40 ms of simulated work plus the database call).

## Impact

The gateway waits on orders, so gateway latency rises too; past 2 s the gateway times out with 504.

## Diagnosis

1. Where is the time spent? Queueing or working:
   - Queue wait: `histogram_quantile(0.95, sum by (le) (rate(orders_queue_wait_seconds_bucket[1m])))`
   - Worker utilization: `orders_worker_utilization`
2. High queue wait with utilization near 1: saturation. Check the request rate against baseline.
3. Low queue wait but slow requests: the work itself is slow. Check for a recent deploy (`service_build_info`) and the database pool (`orders_db_pool_in_use`).
4. CPU: `rate(process_cpu_seconds_total{service="orders"}[1m])` close to 0.5 means the container is at its CPU limit.

## Mitigation

**What the agent does:** scales orders out for saturation; proposes a rollback for a slow release.

**Manually:**

- Scale out: `docker compose up -d --scale orders=2 --no-recreate`
- Clear an injected latency fault: `python -m tools.cli chaos-clear`
- Roll back: `python -m tools.cli deploy --service orders --version v1`

## Escalation

Slow queries: data-team. Already at `max_replicas`: platform-team.

## Related

[saturation](saturation.md), [gateway-latency](gateway-latency.md), [orders-availability](orders-availability.md)
