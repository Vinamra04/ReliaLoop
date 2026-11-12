# Runbook: service-down

**Alert:** `ServiceDown` (page, symptom).

## Summary

A managed service (gateway or orders) has had no healthy instance for 15 seconds: either every instance fails its scrape, or the service has disappeared from service discovery entirely (for example its only container is restarting).

## Impact

- gateway down: all user requests fail.
- orders down: every gateway API request returns 502.

## Diagnosis

1. Container state: `python -m tools.cli ps`. Look for `Restarting`, `Exited` or `unhealthy`.
2. Why did it stop? `python -m tools.cli logs <service>` and `docker inspect <container> --format '{{.State.ExitCode}} {{.State.OOMKilled}}'`.
   - Exit code 1 shortly after start, repeatedly: see [crash-loop](crash-loop.md).
   - `OOMKilled=true`: see [memory-exhaustion](memory-exhaustion.md).
3. Is it really down or only unscrapable? `curl http://localhost:8000/healthz` for the gateway. If the service answers, the problem is the monitoring path: see [pipeline-health](pipeline-health.md).
4. Dependencies: orders needs Postgres to become ready.

## Mitigation

**What the agent does:** diagnoses from restarts, deploy history and logs. A crash loop after a deploy leads to a rollback proposal; otherwise it restarts the service once and escalates if that does not help.

**Manually:**

- Start it: `python -m tools.cli up`
- Roll back a crashing release: `python -m tools.cli deploy --service orders --version v1`

## Escalation

If the service will not start on the last known good version, escalate to platform-team with the container logs.

## Related

[crash-loop](crash-loop.md), [memory-exhaustion](memory-exhaustion.md), [gateway-availability](gateway-availability.md)

Note: while `ServiceDown` fires, latency burn alerts for the same service are suppressed (inhibition rule 2).
