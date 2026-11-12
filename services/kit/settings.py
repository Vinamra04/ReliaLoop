"""Base settings shared by every demo service (12-factor: environment only)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ReleaseProfile = Literal["good", "bad", "crash"]


class ServiceSettings(BaseSettings):
    """Validated at startup so a misconfigured container fails fast."""

    model_config = SettingsConfigDict(extra="ignore")

    service_name: str
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    release_version: str = "dev"
    release_profile: ReleaseProfile = "good"
    chaos_token: str = ""

    # Behaviour of the simulated bad releases (see kit.release).
    bad_release_error_rate: float = Field(default=0.25, ge=0, le=1)
    bad_release_latency_ms: int = Field(default=300, ge=0)
    crash_release_after_s: float = Field(default=8.0, gt=0)

    # Default lifetime of an injected fault when the caller gives none.
    chaos_default_duration_s: int = Field(default=300, gt=0)
    chaos_max_duration_s: int = Field(default=3600, gt=0)
