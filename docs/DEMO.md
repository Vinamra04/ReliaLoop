# ReliaLoop demo walkthrough (Windows)

A step-by-step guide for someone who did not write this code. Every command
below was run on Windows 11 with Docker Desktop on 2026-11-13, and the
"what you should see" text is copied from those runs.

Run every command in **PowerShell**, from the project folder (the one that
contains `docker-compose.yml`). The whole project is driven by one tool:

```powershell
.venv/Scripts/python.exe -m tools.cli <command>
```

Contents: [1. Start](#1-start-the-stack-and-check-health) ·
[2. URLs](#2-urls-and-ports) · [3. Dashboards](#3-the-grafana-dashboards-in-plain-language) ·
[4. Demo A: error spike](#4-demo-a-an-error-spike-fixed-automatically) ·
[5. Demo B: rollback approval](#5-demo-b-a-bad-release-and-the-rollback-approval) ·
[6. Clean up](#6-clear-faults-reset-and-stop) · [7. Tests](#7-run-the-tests) ·
[8. If something goes wrong](#8-if-something-goes-wrong)

---

## 1. Start the stack and check health

Docker Desktop must be running first (whale icon in the system tray).

**One-time setup** (skip if `.venv` and `.env` already exist):

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
.venv/Scripts/python.exe -m tools.cli init
.venv/Scripts/python.exe -m tools.cli build
```

`init` creates `.env` with random tokens. It is safe to run again; it prints
`.env kept (existing values preserved)`. `build` builds the Docker images and
takes a few minutes the first time.

**Start everything:**

```powershell
.venv/Scripts/python.exe -m tools.cli up
```

You should see each container go `Started` then `Healthy`, ending with:

```
stack is healthy
  gateway     http://127.0.0.1:8000
  agent       http://127.0.0.1:8080
  prometheus  http://127.0.0.1:9090
  alertmanager http://127.0.0.1:9093
  grafana     http://127.0.0.1:3001
```

This takes about 30 seconds.

**Check the containers:**

```powershell
.venv/Scripts/python.exe -m tools.cli ps
```

You should see 8 containers, all `Up ... (healthy)`: `agent`, `alertmanager`,
`gateway`, `grafana`, `loadgen`, `orders`, `postgres`, `prometheus`.

**Check the alerting pipeline end to end:**

```powershell
.venv/Scripts/python.exe -m tools.cli verify-pipeline
```

You should see six lines that all start with `ok`:

```
ok    Prometheus targets are up: 6 targets up
ok    Prometheus rules evaluate without errors: 148 rules healthy
ok    Watchdog reaches Alertmanager and is routed to null: ...
ok    Agent webhook rejects requests without a token: request without token got 401
ok    Agent webhook accepts the bearer token: request with token got 202
ok    A page sent to Alertmanager is delivered to the agent: test alert delivered to the agent after about 5s
```

**Check the agent's mode:**

```powershell
.venv/Scripts/python.exe -m tools.cli mode
```

You should see `agent mode: dry_run  diagnosis: rules  queue: 0` (or `observe`).
The three modes are:

| Mode | What the agent does |
|---|---|
| `observe` | Diagnoses incidents, changes nothing. The safe resting state. |
| `dry_run` | Diagnoses and records what it *would* do, changes nothing. Default after a restart. |
| `enforce` | Actually restarts, scales or rolls back services (through the safety checks). |

A mode change lasts until the agent container restarts; after that it goes
back to `AGENT_MODE` in `.env`.

---

## 2. URLs and ports

Everything listens on your own machine only (`127.0.0.1`).

| What | URL | What it is for |
|---|---|---|
| Grafana | http://localhost:3001 | Dashboards. No login needed (read-only viewer). |
| Prometheus | http://localhost:9090 | Stores the metrics and evaluates alert rules. **Alerts** tab shows every rule and whether it is firing. |
| Alertmanager | http://localhost:9093 | Receives firing alerts, groups them and sends them to the agent. |
| Agent: incidents | http://localhost:8080/incidents | Every incident the agent handled, as JSON. |
| Agent: tickets | http://localhost:8080/tickets | Tickets the agent wrote. |
| Agent: pending approvals | http://localhost:8080/actions/pending | Risky actions waiting for a human. |
| Agent: a runbook | http://localhost:8080/runbooks/orders-availability | What an alert means and what to do. |
| Gateway (the demo app) | http://localhost:8000/api/orders | The API real users would call. |
| Load generator | http://localhost:8002/status | Synthetic traffic; 20 requests per second by default. |

Grafana is on **3001** on this machine because `.env` sets `GRAFANA_PORT=3001`
(port 3000 was taken). If your `.env` does not set it, Grafana is on 3000.
`up` always prints the real URL.

The demo system is tiny on purpose: a **load generator** sends orders to the
**gateway**, which forwards them to the **orders** service, which stores them
in **Postgres**. Everything else is the reliability tooling around it.

---

## 3. The Grafana dashboards in plain language

Open http://localhost:3001, then **Dashboards** in the left menu. There are four.
Set the time range (top right) to **Last 15 minutes** and refresh to **5s**.

### 3.1 Service Golden Signals

http://localhost:3001/d/relialoop-golden-signals — "is the service healthy?"
Pick `gateway` or `orders` in the **service** dropdown at the top.

| Panel | Plain meaning | Healthy looks like |
|---|---|---|
| Request rate | Requests per second right now | about 20 |
| Error ratio | Share of requests that failed (HTTP 5xx) | 0%, or "No data" when nothing has failed recently |
| p95 latency | 95 of 100 requests are faster than this | well under 300 ms |
| Replicas up | How many copies of the service are running | 1 |
| Restarts (10m) | How often the process restarted recently | 0 |
| Version | Which release is running | `v1` |
| Request rate by status | Traffic split by status code; red 500s appear during a fault | all 200/201 |
| Request rate by route | Traffic per API endpoint | steady lines |
| Latency percentiles | Typical (p50), slow (p95) and slowest (p99) requests, with the SLO lines at 300 and 500 ms | below the lines |
| Requests in flight | Requests being handled at this instant | low single digits |
| Resident memory | RAM used; the containers are limited to 192 MiB | flat |
| CPU usage | CPU cores used | low, flat |
| Worker utilization (orders) | How busy the orders workers are; above 0.9 means overloaded | around 0.2 |
| Queue wait p95 (orders) | How long a request waited for a free worker | near 0 |
| Database pool (orders) | Postgres connections in use versus open | in use far below open |
| Loadgen requests / latency | The same story as seen by the clients | all `ok` |

### 3.2 SLO and Error Budget

http://localhost:3001/d/relialoop-slo — "are we keeping our promises?"

An **SLO** is a promise such as "99.5% of gateway requests succeed". The
**error budget** is the 0.5% that is allowed to fail. The **burn rate** says
how fast the budget is being spent: 1 means exactly on budget, 14.4 means
14.4 times too fast, which pages.

| Panel | Plain meaning | Healthy looks like |
|---|---|---|
| SLI (5m) | Share of good requests in the last 5 minutes | 100% |
| Objective | The promise | 99.5% (availability) or 95% (latency) |
| Error budget remaining | 1 = untouched, 0 = all spent, negative = overspent (counted over 1 hour in this demo) | near 1 |
| Burn rate (2m) | How fast the budget is being spent | 0 |
| Traffic (2m) | Requests per second; alerts are muted when there is no traffic | about 20 |
| Firing burn alerts | Burn-rate alerts firing for this SLO | 0 |
| SLI vs objective | The SLI over time against the promise | line above the objective |
| Fast and slow burn (page) | The burn rates that wake someone up, against their thresholds (14.4 and 6) | flat at 0 |
| Ticket and trickle burn (ticket) | Slower burns that only create a ticket (thresholds 3 and 1) | flat at 0 |
| Error budget remaining by SLO / Fast-window burn rate by SLO / Firing SLO alerts | The same for all four SLOs at once | budgets near 1, no alerts |

After a demo fault the budget goes **negative** and climbs back over the next
hour. That is expected: the demo deliberately spends the whole budget.

### 3.3 Incidents and Agent

http://localhost:3001/d/relialoop-agent — "what is the agent doing?" The agent
is monitored like any other service.

| Panel | Plain meaning |
|---|---|
| Mode | observe, dry_run or enforce |
| Queue depth | Incidents waiting to be processed (normally 0) |
| LLM errors (1h) | Failed LLM calls; each one fell back to the rule engine |
| Incidents (1h) / Escalations (1h) | Incidents finished / handed to a human in the last hour |
| Recovery success (1h) | Share of actions after which recovery was confirmed |
| Alerts received | Alerts delivered by Alertmanager |
| Incidents by final state | How incidents ended (RESOLVED, ESCALATED, OBSERVED, ...) |
| Incident duration | Time from incident opened to finished |
| Diagnoses by category | What the agent decided was wrong (ERROR_SPIKE, CRASH_LOOP, ...) |
| Validation failures by reason | Proposals the validator rejected as unsafe or unjustified |
| Actions by outcome | executed, blocked, pending_approval, rejected, dry_run, failed |
| Verification results | Whether the fix was confirmed to have worked |
| LLM calls / latency / tokens | Cost and speed of the LLM (only moves in LLM mode) |

### 3.4 Pipeline Health

http://localhost:3001/d/relialoop-pipeline — "can we trust the alerting itself?"

| Panel | Plain meaning | Healthy looks like |
|---|---|---|
| Watchdog | An alert that fires on purpose all the time; if it stops, alerting is broken | 1 |
| Targets up / down | Services Prometheus can / cannot read metrics from | 6 up, 0 down |
| Firing pages / tickets | Urgent / non-urgent alerts firing now | 0 |
| Failed notifications (5m) | Alerts Alertmanager could not deliver | 0 |
| Target health, Scrape duration | Per-service detail of the above | all 1, well under 4 s |
| Rule group evaluation time / failures | Whether alert rules are being computed in time | under 5 s, 0 failures |
| Alerts by state, Notifications sent / failed | What Alertmanager holds and what it delivered to the agent | no failures |

---

## 4. Demo A: an error spike, fixed automatically

You will make half of the `orders` requests fail and watch the system detect,
diagnose, fix and verify. It takes about 90 seconds.

**Before you start**, open three browser tabs:

1. Golden Signals with service = `orders`: http://localhost:3001/d/relialoop-golden-signals
2. SLO and Error Budget: http://localhost:3001/d/relialoop-slo
3. Prometheus alerts: http://localhost:9090/alerts

**Step 1. Let the agent act, using the rule engine:**

```powershell
.venv/Scripts/python.exe -m tools.cli mode enforce --diagnosis rules
```

You should see: `agent mode: enforce  diagnosis: rules  queue: 0`

**Step 2. Inject ONE fault:**

```powershell
.venv/Scripts/python.exe -m tools.cli chaos errors --service orders --rate 0.5
```

You should see `fault injected: errors on orders` and a JSON block with
`"rate": 0.5` and `"remaining_s": 300.0` (the fault removes itself after 5 minutes
even if nothing else happens).

**Step 3. Watch (about 20 seconds):**

- *Golden Signals*: **Error ratio** jumps towards 50% and red 500s appear in
  **Request rate by status**.
- *SLO and Error Budget*: **Burn rate (2m)** shoots far above 14.4 and
  **Error budget remaining** drops.
- *Prometheus alerts*: `SLOBurnRateFast` turns yellow (pending) then red (firing).
- *Alertmanager* (http://localhost:9093): the alert appears, grouped by service.

**Step 4. See the incident:**

```powershell
.venv/Scripts/python.exe -m tools.cli incidents list
```

About 20 seconds after the injection there are two new rows at the top (one
for the `orders` SLO, one for the `gateway` SLO, because users of the gateway
see the errors too). In my run they went `VERIFYING` and then:

```
│ INC-20261113-044 │ gateway │ gateway-availability │ RESOLVED │ ERROR_SPIKE │ orders │ enforce/rules │
│ INC-20261113-043 │ orders  │ orders-availability  │ RESOLVED │ ERROR_SPIKE │ orders │ enforce/rules │
```

If the table is cut off, make the PowerShell window wider and run it again.

**Step 5. Read what the agent did** (use your own incident id):

```powershell
.venv/Scripts/python.exe -m tools.cli incidents show INC-20261113-043
```

You should see the diagnosis, the evidence it relied on, the action and a
timeline like this (times are UTC):

```
INC-20261113-043  RESOLVED  (closed)
  category ERROR_SPIKE  root cause orders
  diagnosis (rules): confidence 0.70, proposes restart_service on orders
    orders is failing 16% of requests with no recent deploy, no restarts and normal load; ...
    evidence: err_ratio_1m@orders, seconds_since_last_deploy@orders, restarts_2m@orders, request_rate_1m@orders
  action restart_service on orders: executed (policy allow / low_risk_auto)
  timeline:
    18:00:52  RECEIVED            incident opened by SLOBurnRateFast
    18:00:52  ENRICHED            collected 28 evidence items from orders
    18:00:52  DIAGNOSED           ERROR_SPIKE at orders (confidence 0.70, rules)
    18:00:52  VALIDATED           proposal valid: restart_service on orders
    18:00:52  POLICY_DECIDED      policy: allow (low_risk_auto)
    18:00:52  ACTING              executing restart_service on orders
    18:01:00  ACTING              restart_service succeeded: restarted ['relialoop-orders-1']: 1 container(s) healthy
    18:01:00  VERIFYING           verifying recovery after restart_service
    18:01:40  RESOLVED            recovery verified: healthy for 3 consecutive checks
    18:01:40  RESOLVED            postmortem written: INC-20261113-043.md
  audit chain: intact (273 rows)
```

How to read it: the alert arrived, the agent collected **evidence** (metrics,
logs, deploy history), the rule engine **diagnosed** an error spike, the
**validator** checked the proposal against the facts, the **policy engine**
allowed it because a restart is low risk, the agent **restarted** orders, then
watched until the service was healthy for three checks in a row.

In my run the fault was injected at 18:00:32, the incident opened at 18:00:52
(20 s) and recovery was verified at 18:01:40 (68 s).

**Step 6. See recovery on the dashboards:** Error ratio is back to 0%, the
burn rate falls back to 0, `SLOBurnRateFast` is green again. The error budget
stays low or negative for up to an hour; that is the cost of the incident.

**Step 7. Read the ticket:**

```powershell
.venv/Scripts/python.exe -m tools.cli tickets --limit 4
```

You should see one ticket per incident with status `resolved`, for example
`orders-availability: error budget burning at more than 14.4x (2m/30s windows)`.

**Step 8. Read the postmortem.** It is a file in the `postmortems` folder:

```powershell
notepad postmortems\INC-20261113-043.md
```

or in the browser: http://localhost:8080/incidents/INC-20261113-043/postmortem

It contains the summary, impact, detection and mitigation times, the diagnosis
with evidence, the action and who decided it (`policy (automatic)`), the
verification result and the full timeline. The "what went well / badly"
sections are left for a human reviewer in rules mode.

---

## 5. Demo B: a bad release and the rollback approval

A restart is low risk, so the agent does it alone. A **rollback is high risk**,
so the agent must wait for a human. This demo shows that step. Stay in
`enforce` mode from Demo A.

**Step 1. Try to deploy a release that crashes on purpose:**

```powershell
.venv/Scripts/python.exe -m tools.cli gate --service orders
```

Right after Demo A you should see the **error-budget gate block the deploy**:

```
  orders-availability      budget remaining -603.8%
gate: blocked: orders-availability has -604% of its error budget left (minimum 25%)
```

That is the gate working: no risky changes while the budget is spent. For the
demo, override it with a reason (the reason is recorded):

```powershell
.venv/Scripts/python.exe -m tools.cli deploy --service orders --version v3-crash --override "demo: show rollback approval"
```

You should see `gate: OVERRIDE: ...` and finally `ok: deployed orders v1 -> v3-crash`.
The `v3-crash` release exits about 8 seconds after every start.

**Step 2. Wait about 50 seconds, then look for the pending approval:**

```powershell
.venv/Scripts/python.exe -m tools.cli incidents list
```

The newest incident is in state `PENDING_APPROVAL` with category `CRASH_LOOP`.
You can also open http://localhost:8080/actions/pending and see
`"type":"rollback_service","target":"orders","params":{"to_version":"v1"},"risk":"high"`.

Nothing has been changed yet. The service keeps crashing until you decide.

**Step 3. Approve it** (use your own incident id):

```powershell
.venv/Scripts/python.exe -m tools.cli approve INC-20261113-045
```

You should see `INC-20261113-045: approve recorded, state PENDING_APPROVAL`.
(To refuse instead, use `reject INC-...`; the incident is then handed to a human.)

**Step 4. About a minute later, read the incident:**

```powershell
.venv/Scripts/python.exe -m tools.cli incidents show INC-20261113-045
```

```
  diagnosis (rules): confidence 0.90, proposes rollback_service on orders
    orders is restarting repeatedly and release v3-crash was deployed 24s ago; rolling back to v1.
  action rollback_service on orders: executed (policy approval_required / high_risk_needs_approval)
    18:04:53  POLICY_DECIDED      policy: approval_required (high_risk_needs_approval)
    18:04:53  PENDING_APPROVAL    waiting for human approval of rollback_service on orders
    18:05:29  PENDING_APPROVAL    rollback_service approved by operator
    18:05:36  ACTING              rollback_service succeeded: rolled orders back relialoop/orders:v3-crash -> relialoop/orders:v1: 1 container(s) healthy
    18:06:16  RESOLVED            recovery verified: healthy for 3 consecutive checks
```

On the Golden Signals dashboard (service = `orders`) the **Version** panel
shows `v3-crash` and then `v1` again, and **Restarts** shows the crashes.

---

## 6. Clear faults, reset and stop

**Remove every injected fault** (safe to run at any time):

```powershell
.venv/Scripts/python.exe -m tools.cli chaos-clear
```

You should see `cleared faults on orders` and `cleared faults on gateway`.

**Put the agent back into observe mode** (it will no longer change anything):

```powershell
.venv/Scripts/python.exe -m tools.cli mode observe
```

You should see `agent mode: observe  diagnosis: rules  queue: 0`.

**If you changed the traffic rate**, set it back to the baseline:

```powershell
.venv/Scripts/python.exe -m tools.cli traffic --rps 20
```

**Stop everything:**

```powershell
.venv/Scripts/python.exe -m tools.cli down
```

This stops and removes only this project's containers. Metrics, incidents and
dashboards are kept in Docker volumes, so `up` brings everything back. After
`down` and `up` the agent starts in the mode set by `AGENT_MODE` in `.env`
(`dry_run` by default), never in `enforce`.

---

## 7. Run the tests

None of these need the stack to be running, except `verify-pipeline`.
`slo validate` and `test-rules` need Docker Desktop (they run `promtool` and
`amtool` inside the official images).

```powershell
.venv/Scripts/python.exe -m pytest -q
```

Unit tests. You should see a last line like `489 passed in 28s`. The LLM is
always a scripted fake here; no API key is used.

```powershell
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check .
.venv/Scripts/python.exe -m mypy
```

Lint, formatting and type checks. You should see `All checks passed!`,
`160 files already formatted` (the count grows with the code) and
`Success: no issues found in 65 source files`.

```powershell
.venv/Scripts/python.exe -m tools.cli slo validate
.venv/Scripts/python.exe -m tools.cli test-rules
docker compose config -q
```

`slo validate` checks that the generated alert rules match the SLO specs and
that Prometheus accepts them (`ok    promtool check rules`, `ok    promtool check config`).
`test-rules` runs the alert-rule unit tests and the Alertmanager routing tests;
every line starts with `ok`. `docker compose config -q` prints nothing when
the Compose file is valid.

---

## 8. If something goes wrong

| Symptom | What to do |
|---|---|
| `up` fails or commands print connection errors | Docker Desktop is not running, or the stack is down. Start Docker Desktop, then run `up` again. |
| A container is not `healthy` | `.venv/Scripts/python.exe -m tools.cli logs <service> --tail 50` (for example `logs agent`). |
| Grafana does not open on 3001 | Use the Grafana URL that `up` printed; the port comes from `GRAFANA_PORT` in `.env`. |
| No incident appears after a fault | Check `mode` (in `observe` the agent only diagnoses), then `verify-pipeline`. |
| `deploy` says `gate: blocked` | The error budget is spent (expected after a demo). Wait up to an hour or use `--override "reason"`. |
| The agent proposed something but did nothing | It is in `dry_run` or `observe`. Run `mode enforce` for the demo. |
| The machine gets slow | The stack uses about 700 MB of RAM. Run `down` when you are finished. |
