"""Canonical repository paths, so no tool hard-codes a location twice."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
SECRETS_DIR = REPO_ROOT / ".secrets"
WEBHOOK_TOKEN_FILE = SECRETS_DIR / "webhook_token"
VERSIONS_FILE = REPO_ROOT / "deploy" / "versions.env"
DASHBOARDS_DIR = REPO_ROOT / "observability" / "grafana" / "dashboards"
SLO_DIR = REPO_ROOT / "slo"
SLO_SCHEMA_FILE = SLO_DIR / "schema.json"
CMDB_FILE = REPO_ROOT / "cmdb" / "services.yaml"
PROMETHEUS_DIR = REPO_ROOT / "observability" / "prometheus"
GENERATED_RULES_DIR = PROMETHEUS_DIR / "rules" / "generated"
ALERTMANAGER_DIR = REPO_ROOT / "observability" / "alertmanager"
SLO_DESIGN_DOC = REPO_ROOT / "docs" / "slo-design.md"
RUNTIME_DIRS = (
    REPO_ROOT / "postmortems",
    REPO_ROOT / "eval" / "results",
    REPO_ROOT / "data",
)
