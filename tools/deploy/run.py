"""Deploy a service version through the error-budget gate.

Steps: gate -> record `started` -> write `deploy/versions.env` -> `docker
compose up -d --no-deps <service>` -> wait for health -> record `completed`
or `failed`. The previous version is recorded with every event, which is what
lets the agent roll back.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from libs.core.gate import GateDecision
from tools.init_env import parse_env

WAIT_TIMEOUT_S = 60


@dataclass(frozen=True)
class DeployResult:
    ok: bool
    exit_code: int
    message: str
    gate: GateDecision
    previous_version: str | None = None


def version_key(service: str) -> str:
    return f"{service.upper().replace('-', '_')}_VERSION"


def current_version(versions_file: Path, service: str) -> str | None:
    if not versions_file.exists():
        return None
    return parse_env(versions_file.read_text(encoding="utf-8")).get(version_key(service))


def write_version(versions_file: Path, service: str, version: str) -> None:
    """Set the service's version line, keeping comments and other services."""
    key = version_key(service)
    lines = versions_file.read_text(encoding="utf-8").splitlines() if versions_file.exists() else []
    replaced = False
    for index, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[index] = f"{key}={version}"
            replaced = True
    if not replaced:
        lines.append(f"{key}={version}")
    versions_file.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


class EventRecorder:
    """Sends deploy events to the agent; falls back to a local file if it is down."""

    def __init__(self, client: httpx.Client, agent_url: str, token: str, fallback: Path) -> None:
        self._client = client
        self._url = f"{agent_url.rstrip('/')}/events/deploy"
        self._token = token
        self._fallback = fallback
        self.used_fallback = False

    def record(self, event: dict[str, Any]) -> None:
        try:
            response = self._client.post(
                self._url, json=event, headers={"Authorization": f"Bearer {self._token}"}
            )
            response.raise_for_status()
        except httpx.HTTPError:
            self.used_fallback = True
            self._fallback.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps({**event, "ts": datetime.now(UTC).isoformat()})
            with self._fallback.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")


def deploy(
    *,
    service: str,
    version: str,
    gate: GateDecision,
    versions_file: Path,
    recorder: EventRecorder,
    compose_up: Callable[[str], bool],
    image_exists: Callable[[str], bool],
    actor: str = "cli",
) -> DeployResult:
    """Run the deploy. `compose_up(service)` returns whether the service became healthy."""
    if not gate.allowed:
        return DeployResult(False, gate.exit_code, gate.reason, gate)
    image = f"relialoop/{service}:{version}"
    if not image_exists(image):
        return DeployResult(False, 1, f"image {image} does not exist; run `build` first", gate)

    previous = current_version(versions_file, service)
    event: dict[str, Any] = {
        "service": service,
        "version": version,
        "previous_version": previous,
        "actor": actor,
    }
    if gate.overridden or gate.failed_open:
        event["override_reason"] = gate.reason[:300]
    recorder.record({**event, "status": "started"})
    write_version(versions_file, service, version)
    healthy = compose_up(service)
    recorder.record({**event, "status": "completed" if healthy else "failed"})
    if healthy:
        return DeployResult(True, 0, f"deployed {service} {previous} -> {version}", gate, previous)
    return DeployResult(
        False,
        1,
        f"{service} {version} was started but did not become healthy within {WAIT_TIMEOUT_S}s",
        gate,
        previous,
    )
