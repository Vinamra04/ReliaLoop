# ReliaLoop build progress

Read this first at the start of every session; resume from the first unchecked item.
The plan is `CLAUDE.md`. On this machine use `.venv/Scripts/python.exe -m tools.cli …` (no `make`).

## Phase 0 — Bootstrap
- [x] 0.1 Environment detection (Windows 11, Docker 29.9.3, Compose v5.7.0, Python 3.14.2, git 2.47, Node 22.16, no make)
- [x] 0.2 git config files: `.gitignore`, `.gitattributes`, `.editorconfig`, `LICENSE`, `pyproject.toml`, `.pre-commit-config.yaml`
- [x] 0.3 `PROGRESS.md`, `.env.example`, Makefile + `tools/cli.py` skeleton, ADR template + ADR 0001 (image tags verified)
- [x] 0.4 venv, dev tools, pre-commit hooks installed; `pre-commit run --all-files` passes; first commit; tag `phase-0-complete`

## Phase 1 — Services and observability base
- [x] 1.1 `services/kit` (settings, logging, metrics middleware, health, chaos)
- [x] 1.2 `gateway`, `orders`, `loadgen` + Dockerfiles + unit tests
- [x] 1.3 `docker-compose.yml` (networks, healthchecks, limits, labels, localhost ports)
- [x] 1.4 Prometheus config (jobs, relabeling, dns_sd) + Grafana provisioning + dashboard 1
- [x] 1.5 Chaos endpoints + release-profile images (`v1`, `v2-bad`, `v3-crash`)
- [x] 1.6 Tune orders capacity model; document in `docs/configuration.md`
- [x] 1.7 Done-when checks shown; tag `phase-1-complete`

## Phase 2 — SLOs, alerting, Alertmanager
- [x] 2.1 SLO specs, pydantic models, generator, validator, promtool integration
- [x] 2.2 Generated + handwritten rules; `promtool test rules` suites
- [x] 2.3 Alertmanager config (routes, receivers, 3 inhibitions), credentials file, amtool tests, webhook sink
- [x] 2.4 Dashboards 2 (SLO & Error Budget) and 4 (Pipeline Health)
- [x] 2.5 `verify-pipeline`, `silence`, runbooks, `docs/slo-design.md`, `docs/alerting.md`
- [x] 2.6 Done-when checks shown; tag `phase-2-complete`

## Phase 3 — Agent core, rule-based mode
- [x] 3.1 Package structure, ports, config, store + migrations, CMDB, state machine, queue/workers, webhooks, metrics
- [x] 3.2 Evidence collector (catalog, Prometheus adapter, log summariser, redaction, deploy history)
- [x] 3.3 Rule-based engine, validator, policy engine, persisted rate limits
- [x] 3.4 `DockerExecutor`: restart, scale, rollback
- [x] 3.5 Verification loop, escalation, tickets, deploy event endpoint, deploy tool + gate
- [x] 3.6 Agent Dockerfile + compose service; dashboard 3; agent health alerts + runbook
- [x] 3.7 Done-when checks shown; tag `phase-3-complete`

## Phase 4 — LLM diagnosis, approval flow, postmortems
- [x] 4.1 Gemini key verified (masked); 27 models listed; `gemini-3.6-flash` chosen (newest flash-class model that answers); ADR 0003
- [ ] 4.2 LLM abstraction: Gemini, Ollama, Fake, breaker + fallback, retries, dev cache, metrics
- [ ] 4.3 Prompts (versioned), schema, bounded evidence loop, validator hardening, prompt-injection tests
- [ ] 4.4 Re-plan on failed verification, re-adoption on restart, kill switch
- [ ] 4.5 Postmortem generator, tickets, GET endpoints, runbook serving
- [ ] 4.6 Done-when checks shown; tag `phase-4-complete`

## Phase 5 — Evaluation, CI, quality gates
- [ ] 5.1 Eval harness (scenarios, injector, measurer, report, charts; resumable; always cleans up)
- [ ] 5.2 Real `--quick` run; record durations
- [ ] 5.3 CI workflow, Dependabot, `smoke`, coverage thresholds
- [ ] 5.4 Coverage raised to thresholds; lint/type pass
- [ ] 5.5 Done-when checks shown; tag `phase-5-complete`

## Phase 6 — Mission Control console
- [ ] 6.1 Agent additions: SSE stream, `/admin/deploy`, `results.json`, `docs/metrics-catalog.json`
- [ ] 6.2 Console API (BFF) + tests + Docker build + compose service
- [ ] 6.3 Design system, base components, `/design` route, `docs/ui-design.md`
- [ ] 6.4 Views: Overview, Incidents, Fault Lab (+Eval viewer), SLOs, Agent, Topology, Alerts, Deploys & Gate, Services, Metrics Explorer
- [ ] 6.5 Polish: motion, states, accessibility, reduced motion, performance, light theme
- [ ] 6.6 Vitest, Playwright, axe, bundle-size check, screenshots
- [ ] 6.7 Done-when checks shown; tag `phase-6-complete`

## Phase 7 — Documentation, polish, final verification
- [ ] 7.1 All docs, runbooks, ADRs, README with real screenshots and honest results
- [ ] 7.2 Clean-state `init` + `demo` reproducibility run
- [ ] 7.3 Final secret scan, clean `git status`, tag `v1.0.0`
- [ ] 7.4 Handoff report (`docs/HANDOFF.md`)

## Decisions / Deviations
- **No `make` on the dev machine.** The Python CLI is canonical (as the plan allows). The Makefile is written but has not been executed here; targets are added only once the command they wrap exists.
- **CLI commands are added phase by phase** rather than stubbed up front, so `--help` never lists a command that does not work (no dead code).
- **Host Python is 3.14.2** (images use 3.12). Code targets 3.11+ syntax; ruff `target-version = py311`, mypy `python_version = 3.12`.
- **Image tags** chosen from Docker Hub's current stable releases on 2026-11-12 and verified with `docker manifest inspect`; see `docs/adr/0001-stack-and-versions.md`.
- **Extra pre-commit hook:** a local `tools/secret_scan.py` hook for API-key shapes (§2.4), in addition to the hooks the plan lists.

- **Compose container names.** Orders has no fixed `container_name` (Compose refuses to scale a service that has one), so containers are `relialoop-orders-1`, `-2`, …; the agent will identify containers by the labels `com.docker.compose.service` / `relialoop.service`, not by a fixed name.
- **Orders images are built by build-only Compose services** (`orders-v1`, `orders-v2-bad`, `orders-v3-crash`, profile `build`). The running `orders` service has no `build:` section, so a build can never tag a good image with a bad version name. `python -m tools.cli build` builds everything.
- **Chaos for orders goes through the gateway** (`/chaos/target/orders/...`, fanned out to every replica), because orders replicas are not published to the host.
- **Faults are applied by a FastAPI dependency, not middleware**, so injected errors are recorded against the real route template and count in the SLIs.
- **Leaked memory is kept after a leak fault expires** (growth stops; only `DELETE /chaos` or a restart frees it), like a real leak.
- **Orders sheds load**: a request waiting more than 1.5 s for a worker slot gets 503 (gateway reports 502). Under saturation errors are therefore mostly 502, not 504 as the plan's taxonomy suggests.
- **Saturation surge is 150 rps, not 180**: measured capacity is about 89 rps per replica, so 150 rps saturates one replica and is healthy on two (scale +1 recovers). See `docs/configuration.md`.
- **Loadgen spreads requests over 8 HTTP client pools and has 2 CPUs**: a single httpx pool with hundreds of connections stalled the generator under overload (found while tuning).
- **Dashboards are generated** from `tools/dashboards/definitions.py` (`dashboards generate`, with a staleness test) instead of hand-written JSON.
- **Host ports are configurable** (`*_PORT` in `.env`). On the dev machine port 3000 is used by another program, so the local `.env` sets `GRAFANA_PORT=3001`; the default stays 3000.

- **Alert severities.** `MemoryExhaustionPredicted` pages (only signal before impact); `CrashLoop` and `SaturationHigh` are tickets. Inhibition rule 1 targets only `class=symptom` so cause alerts are never hidden.
- **`ServiceDown` uses a CMDB-generated `service:managed:info` series** (`unless on(service) up == 1`) so it also fires when a restarting container vanishes from DNS discovery.
- **Availability SLI uses `or vector(0)`** so a service with no 5xx series reports 0 instead of no data.
- **Rules for one SLO live in a single group** so alerts see burn rates from the same evaluation cycle.
- **Agent reuses `services/kit`** (logging, metrics middleware, health). The Phase 2 agent is only the authenticated webhook receiver; alerts labelled `relialoop_test=true` are counted but never become incidents.
- **Runbook "what the agent does" sections describe the designed behaviour**; re-check them against the built agent in Phase 7.
- **Webhook token is mounted at `/run/secrets/webhook_token`** in Alertmanager.

- **Restart cooldown lives in the policy engine only**, not also in the validator's consistency matrix: one place for every limit.
- **Runtime mode changes last until the agent restarts.** `AGENT_MODE` in `.env` is the source of truth at startup, so a demo that switched to `enforce` cannot silently stay there.
- **Correlation ("covered").** One root cause often pages for several SLOs. A second incident proposing the action another incident is running (or just ran) verifies its own recovery instead of repeating it; an executed action also covers sibling incidents waiting for approval of the same action.
- **Incident lifecycle.** A verified recovery closes the incident at once. Dry-run, observed, escalated and rejected incidents stay open until their alerts resolve. A late notification of the same alert episode (same fingerprint, `startsAt` before closure) never opens a new incident.
- **Re-adoption after restart:** untouched incidents are processed, verification resumes, approvals keep waiting, anything interrupted mid-action is escalated (`agent_restart`) rather than repeated.
- **Crash detection uses restarts by the container restart policy** (real crashes), falling back to process start-time changes only when the runtime cannot be asked. One crash right after a deploy already counts as a crashing release. Found live: the agent's own restart and a deploy looked like a crash loop, and a crashing release was first mistaken for a gateway error spike.
- **A release that was rolled back is no longer "the last deploy"** for diagnosis or validation (found live: a reverted release was blamed for a later, unrelated fault). Restart evidence uses a 2-minute window (`restarts_2m`), matching the `CrashLoop` alert.
- **The deploy tool treats redeploying the current version as a no-op**, so the recorded previous version (the rollback target) is never overwritten.
- **Agent-created replicas and rolled-back containers keep the Compose labels**, so `docker compose down` removes them and `up` scales back to one replica. After a rollback Compose may recreate the container once on the next `up` (stale config hash); harmless.
- **Docker socket access via `group_add`** (`DOCKER_GID`, 0 on Docker Desktop) so the agent still runs as a non-root user.
- **Agent health alerts are tickets** delivered to the agent's own ticket endpoint; production would route them somewhere that does not depend on the agent.

## Known issues
- `fastapi.testclient` prints a deprecation warning asking for `httpx2`; tests pass. Not changed because the plan locks `httpx`.
- The project lives in a OneDrive-synced folder; OneDrive may sync or briefly lock `.venv` / `node_modules`. No failure observed so far.
