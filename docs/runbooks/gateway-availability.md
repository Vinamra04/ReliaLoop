# Runbook: gateway-availability

**Alerts:** `SLOBurnRateFast`, `SLOBurnRateSlow` (page), `SLOBurnRateTicket`, `SLOBurnRateTrickle` (ticket) with `slo="gateway-availability"`.

## Summary

The gateway is answering too many API requests with 5xx. The objective is 99.5% non-5xx; the alert fires when the error budget burns faster than the tier's factor over both of its windows.

## Impact

Users see failed requests. The gateway is the user-facing edge, so this is direct customer impact. A fast burn spends the whole demo budget in minutes.

## Diagnosis

The gateway rarely fails on its own: it proxies to orders, so start downstream.

1. Which status codes? `sum by (status) (rate(http_requests_total{service="gateway", route=~"/api.*"}[1m]))`
   - 502: orders returned 5xx, shed load (503) or refused the connection.
   - 504: orders did not answer within 2 s.
2. Is orders failing too? Check `orders-availability` and
   `sum by (status) (rate(http_requests_total{service="orders", route=~"/orders.*"}[1m]))`.
3. Is orders restarting? `changes(process_start_time_seconds{service="orders"}[5m])`
4. Is orders saturated? `orders_worker_utilization` above 0.9 and request rate well above baseline.
5. Was there a deploy? `service_build_info{service="orders"}` and `python -m tools.cli incidents list`.
6. Logs: `python -m tools.cli logs gateway` and `python -m tools.cli logs orders`.

Dashboards: Service Golden Signals (gateway, then orders), SLO and Error Budget.

## Mitigation

**What the agent does (enforce mode):** collects the evidence above, localises the cause to the most downstream unhealthy service, and acts on that service: restart for an error spike, scale out for saturation, roll back (after human approval) for a bad deploy or crash loop. It then verifies the SLI recovered.

**Manually:**

- Clear an injected fault: `python -m tools.cli chaos-clear`
- Restart orders: `docker compose restart orders`
- Add an orders replica: `docker compose up -d --scale orders=2 --no-recreate`
- Roll back a deploy: `python -m tools.cli deploy --service orders --version v1`

## Escalation

If the cause is Postgres (owned by data-team) or the agent escalated the incident, contact the owner listed in `cmdb/services.yaml`. Escalated incidents carry a high-priority ticket with the reason.

## Related

[orders-availability](orders-availability.md), [gateway-latency](gateway-latency.md), [service-down](service-down.md), [crash-loop](crash-loop.md), [saturation](saturation.md)
