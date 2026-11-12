# ADR 0001: Stack and pinned versions

- **Status:** accepted
- **Date:** 2026-11-12

## Context

The project must be reproducible from a clean clone, so every image and
dependency is pinned to an exact version. Tags were chosen from the current
stable releases listed by Docker Hub on the date above and each one was
confirmed to exist with `docker manifest inspect` before being written here.

## Decision

Runtime is Docker Compose v2; services are Python 3.12 (FastAPI + uvicorn);
observability is Prometheus, Alertmanager and Grafana, all provisioned as code.

| Image | Pinned tag | Verified |
|---|---|---|
| `prom/prometheus` | `v3.16.0` | `docker manifest inspect` OK, 2026-11-12 |
| `prom/alertmanager` | `v0.35.0` | `docker manifest inspect` OK, 2026-11-12 |
| `grafana/grafana` | `13.3.1` | `docker manifest inspect` OK, 2026-11-12 |
| `postgres` | `16.15-alpine` | `docker manifest inspect` OK, 2026-11-12 |
| `python` | `3.12.16-slim` | `docker manifest inspect` OK, 2026-11-12 |
| `node` (console build stage only) | `22.23.5-alpine` | `docker manifest inspect` OK, 2026-11-12 |

Build environment the project was developed on: Windows 11, Docker Engine
29.9.3 (Docker Desktop), Docker Compose v5.7.0, host Python 3.14.2.

Python dependencies are pinned exactly per component (`requirements.txt` next
to each Dockerfile) and in `requirements-dev.txt` for host tooling. Dependabot
proposes upgrades; `pip-audit` runs in CI.

## Consequences

- Builds are repeatable, at the cost of needing deliberate version bumps.
- Host tooling runs on Python 3.14 while images run 3.12, so code must stay
  compatible with 3.11+ syntax and unit tests run on a newer interpreter than
  production. The Docker smoke test covers the 3.12 runtime.
- `make` is not available on the development machine; the Python CLI
  (`python -m tools.cli`) is the canonical interface and the Makefile is a thin
  wrapper that has not been executed on this machine.
