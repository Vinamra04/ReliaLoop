"""Host-side access to the running stack: Compose invocations and local URLs."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

from tools import paths
from tools.init_env import parse_env

DEFAULT_PORTS = {
    "GATEWAY_PORT": "8000",
    "LOADGEN_PORT": "8002",
    "PROMETHEUS_PORT": "9090",
    "ALERTMANAGER_PORT": "9093",
    "GRAFANA_PORT": "3000",
    "AGENT_PORT": "8080",
    "CONSOLE_PORT": "8088",
}
APP_IMAGES = (
    "relialoop/agent:v1",
    "relialoop/gateway:v1",
    "relialoop/loadgen:v1",
    "relialoop/orders:v1",
    "relialoop/orders:v2-bad",
    "relialoop/orders:v3-crash",
)


class StackError(RuntimeError):
    """A precondition for talking to the stack is not met."""


def load_env() -> dict[str, str]:
    """Return `.env` merged with `deploy/versions.env` (never printed)."""
    if not paths.ENV_FILE.exists():
        raise StackError(".env not found; run `python -m tools.cli init` first")
    values = parse_env(paths.ENV_FILE.read_text(encoding="utf-8"))
    values.update(parse_env(paths.VERSIONS_FILE.read_text(encoding="utf-8")))
    return values


def local_url(port_key: str, env: dict[str, str] | None = None) -> str:
    """Base URL of a published service on localhost."""
    values = env if env is not None else load_env()
    return f"http://127.0.0.1:{values.get(port_key) or DEFAULT_PORTS[port_key]}"


def compose_command(args: Sequence[str]) -> list[str]:
    """Build the `docker compose` command line with both env files."""
    return [
        "docker",
        "compose",
        "--project-directory",
        str(paths.REPO_ROOT),
        "--env-file",
        str(paths.ENV_FILE),
        "--env-file",
        str(paths.VERSIONS_FILE),
        *args,
    ]


def compose(*args: str, check: bool = True) -> int:
    """Run `docker compose …`, streaming its output to the terminal."""
    if not paths.ENV_FILE.exists():
        raise StackError(".env not found; run `python -m tools.cli init` first")
    return subprocess.run(compose_command(args), check=check).returncode  # noqa: S603


def missing_images() -> list[str]:
    """Application images that have not been built yet."""
    missing = []
    for image in APP_IMAGES:
        result = subprocess.run(  # noqa: S603
            ["docker", "image", "inspect", image],  # noqa: S607
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            missing.append(image)
    return missing
