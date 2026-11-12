# Runbook: crash-loop

**Alert:** `CrashLoop` (ticket, cause).

## Summary

A service process restarted at least twice within 2 minutes. This is a cause alert: it explains a symptom (errors, `ServiceDown`) rather than reporting user impact itself, and it is never inhibited.

## Impact

While the process is down or starting, requests fail. With a single replica the service flaps between available and unavailable.

## Diagnosis

1. Restart count: `changes(process_start_time_seconds{service="orders"}[5m])` and `docker inspect <container> --format '{{.RestartCount}}'`.
2. Exit reason: `docker inspect <container> --format '{{.State.ExitCode}} {{.State.OOMKilled}}'`.
3. Last log lines before each exit: `python -m tools.cli logs orders --tail 100`. A startup failure logged right before the exit points at the release.
4. Was there a deploy shortly before the first restart? `service_build_info` and the deploy history. The simulated `v3-crash` release exits about 8 seconds after start.
5. Memory: if RSS climbs to the limit before each exit, it is an out-of-memory kill, not a crashing release.

## Mitigation

**What the agent does:** if a deploy happened recently it proposes rolling back to the previous version; this is high risk and waits for human approval. With no recent deploy it restarts the service once and escalates if the loop continues.

**Manually:**

- Roll back: `python -m tools.cli deploy --service orders --version v1`
- Approve the agent's rollback: `python -m tools.cli approve <incident-id>`

## Escalation

A crash loop on the last known good version is not fixable by rollback: escalate to platform-team.

## Related

[service-down](service-down.md), [memory-exhaustion](memory-exhaustion.md), [orders-availability](orders-availability.md)
