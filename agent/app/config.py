"""Agent settings: environment-driven, validated at startup, fail fast."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_TOKEN_LENGTH = 16

AgentMode = Literal["observe", "dry_run", "enforce"]
DiagnosisMode = Literal["rules", "llm"]


class AgentSettings(BaseSettings):
    """Every default lives here; nothing in the code base hard-codes a tunable."""

    model_config = SettingsConfigDict(extra="ignore")

    service_name: str = "agent"
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    release_version: str = "dev"

    # --- auth ---------------------------------------------------------------
    webhook_token: str = Field(min_length=MIN_TOKEN_LENGTH)
    approver_token: str = Field(min_length=MIN_TOKEN_LENGTH)

    # --- behaviour ----------------------------------------------------------
    # observe: diagnose only. dry_run: decide and record, execute nothing.
    # enforce: execute through validator and policy.
    agent_mode: AgentMode = "dry_run"
    diagnosis_mode: DiagnosisMode = "rules"
    agent_workers: int = Field(default=2, ge=1, le=16)
    agent_queue_size: int = Field(default=100, ge=1)

    # --- LLM ----------------------------------------------------------------
    llm_provider: Literal["gemini", "fake"] = "gemini"
    gemini_api_key: SecretStr = SecretStr("")
    # Empty: pick the newest responsive flash-class model at first use (ADR 0003).
    gemini_model: str = ""
    # Optional JSON file with scripted responses for the fake provider.
    llm_fake_fixture: Path | None = None
    llm_timeout_s: float = Field(default=30.0, gt=0)
    llm_probe_timeout_s: float = Field(default=20.0, gt=0)
    llm_max_attempts: int = Field(default=3, ge=1, le=5)
    llm_max_output_tokens: int = Field(default=4096, gt=0)
    llm_temperature: float = Field(default=0.0, ge=0, le=1)
    # Extra evidence rounds the LLM may request, and the hard cap on API calls.
    max_evidence_rounds: int = Field(default=2, ge=0)
    max_llm_calls_per_incident: int = Field(default=3, ge=1)
    postmortems_dir: Path = Path("/postmortems")

    # --- API limits ---------------------------------------------------------
    max_body_bytes: int = Field(default=262_144, gt=0)
    max_alerts_per_webhook: int = Field(default=100, gt=0)

    # --- dependencies -------------------------------------------------------
    prometheus_url: str = "http://prometheus:9090"
    db_path: Path = Path("/data/agent.db")
    cmdb_path: Path = Path("/app/cmdb/services.yaml")
    policy_path: Path = Path("/app/agent/policy.yaml")
    runbooks_dir: Path = Path("/app/docs/runbooks")
    versions_file: Path = Path("/deploy/versions.env")
    compose_project: str = "relialoop"
    http_timeout_s: float = Field(default=5.0, gt=0)

    # --- evidence -----------------------------------------------------------
    evidence_timeout_s: float = Field(default=10.0, gt=0)
    log_lines: int = Field(default=200, gt=0)
    log_since_s: int = Field(default=300, gt=0)
    log_max_bytes: int = Field(default=4096, gt=0)
    log_tail_lines: int = Field(default=20, gt=0)
    deploy_history_limit: int = Field(default=5, gt=0)

    # --- rule-based diagnosis thresholds -------------------------------------
    error_ratio_elevated: float = Field(default=0.05, gt=0, lt=1)
    latency_p99_elevated_s: float = Field(default=1.0, gt=0)
    restarts_threshold: int = Field(default=2, ge=1)
    deploy_recent_s: int = Field(default=600, gt=0)
    memory_pct_threshold: float = Field(default=0.7, gt=0, lt=1)
    saturation_utilization: float = Field(default=0.9, gt=0, le=1)
    load_surge_factor: float = Field(default=1.5, gt=1)

    # --- validator ----------------------------------------------------------
    min_confidence: float = Field(default=0.6, ge=0, le=1)
    rollback_lookback_s: int = Field(default=1800, gt=0)

    # --- execution and verification -----------------------------------------
    action_timeout_s: float = Field(default=90.0, gt=0)
    verify_wait_s: float = Field(default=20.0, ge=0)
    verify_interval_s: float = Field(default=10.0, gt=0)
    verify_timeout_s: float = Field(default=120.0, gt=0)
    verify_consecutive: int = Field(default=3, ge=1)
    # Recovered when the short-window burn rate is below this share of the
    # fast-burn factor.
    fast_burn_factor: float = Field(default=14.4, gt=0)
    recovery_burn_fraction: float = Field(default=0.5, gt=0, le=1)
    verify_burn_window: str = "30s"
    max_replans: int = Field(default=1, ge=0)
