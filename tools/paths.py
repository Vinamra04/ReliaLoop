"""Canonical repository paths, so no tool hard-codes a location twice."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
SECRETS_DIR = REPO_ROOT / ".secrets"
WEBHOOK_TOKEN_FILE = SECRETS_DIR / "webhook_token"
RUNTIME_DIRS = (
    REPO_ROOT / "postmortems",
    REPO_ROOT / "eval" / "results",
    REPO_ROOT / "data",
)
