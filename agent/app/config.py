"""Agent settings: environment-driven, validated at startup, fail fast."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_TOKEN_LENGTH = 16


class AgentSettings(BaseSettings):
    """Every default lives here; nothing in the code base hard-codes a tunable."""

    model_config = SettingsConfigDict(extra="ignore")

    service_name: str = "agent"
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    release_version: str = "dev"

    webhook_token: str = Field(min_length=MIN_TOKEN_LENGTH)
    approver_token: str = Field(min_length=MIN_TOKEN_LENGTH)

    # Largest request body the API accepts; larger bodies get 413.
    max_body_bytes: int = Field(default=262_144, gt=0)
    # Most alerts accepted in one Alertmanager notification.
    max_alerts_per_webhook: int = Field(default=100, gt=0)
