"""Loadgen settings."""

from __future__ import annotations

from pydantic import Field

from services.kit.settings import ServiceSettings


class LoadgenSettings(ServiceSettings):
    service_name: str = "loadgen"
    port: int = 8002

    target_url: str = "http://gateway:8000"
    loadgen_rps: float = Field(default=20.0, ge=0)
    loadgen_max_rps: float = Field(default=500.0, gt=0)
    loadgen_token: str = ""
    read_fraction: float = Field(default=0.7, ge=0, le=1)
    request_timeout_s: float = Field(default=3.0, gt=0)
    # Open-loop generator: beyond this many in-flight requests new ones are
    # dropped (and counted) instead of piling up without bound.
    max_in_flight: int = Field(default=600, gt=0)
    known_ids: int = Field(default=200, gt=0)
