# Runbook: memory-exhaustion

**Alert:** `MemoryExhaustionPredicted` (page, cause).

## Summary

A service instance uses more than 60% of its memory limit and, at the growth rate of the last minute, will pass 90% of the limit within 60 seconds. Steady growth like this is the signature of a memory leak.

This is the one cause alert that pages: it is the only signal that allows acting *before* users are affected. Once the container hits its limit the kernel kills it and requests fail.

## Impact

None yet. If nothing is done: an out-of-memory kill, a restart, and failed requests while the process is down. With a persistent leak this repeats and becomes a crash loop.

## Diagnosis

1. Memory trend: `process_resident_memory_bytes{service="gateway"}` on the Service Golden Signals dashboard. A straight upward line is a leak; a step is a load change.
2. Growth rate: `deriv(process_resident_memory_bytes{service="gateway"}[1m])` in bytes per second.
3. Limit: `service:memory_limit:bytes` (192 MiB for the demo services).
4. Did it start with a deploy? `service_build_info`. If so, treat it as a bad release.
5. Is load unusually high? Memory that follows request rate is not a leak.
6. Injected fault? `curl -H "X-Chaos-Token: ..." http://localhost:8000/chaos` shows active faults and leaked megabytes.

## Mitigation

**What the agent does:** restarts the affected service, which releases the leaked memory, then verifies that memory is back to normal and the SLI is healthy. A restart only buys time when the leak is in the code.

**Manually:**

- Restart: `docker compose restart gateway`
- Clear an injected leak: `python -m tools.cli chaos-clear`

## Escalation

A leak that returns after a restart needs a code fix: open a ticket for the owning team with the memory graph and the growth rate.

## Related

[crash-loop](crash-loop.md), [service-down](service-down.md)
