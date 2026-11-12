"""Run promtool and amtool from the pinned images.

Neither tool needs to be installed on the host: both ship inside the
Prometheus and Alertmanager images the stack already uses, so the checks run
against exactly the versions that run the rules.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from tools import paths

PROMETHEUS_IMAGE = "prom/prometheus:v3.16.0"
ALERTMANAGER_IMAGE = "prom/alertmanager:v0.35.0"
PROMETHEUS_MOUNT = "/etc/prometheus"
ALERTMANAGER_MOUNT = "/etc/alertmanager"
TIMEOUT_S = 180


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str


def _run(
    image: str, entrypoint: str, mount_from: Path, mount_to: str, args: list[str]
) -> ToolResult:
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--label",
        "relialoop.tool=true",
        "-v",
        f"{mount_from}:{mount_to}:ro",
        "-w",
        mount_to,
        "--entrypoint",
        entrypoint,
        image,
        *args,
    ]
    try:
        done = subprocess.run(  # noqa: S603 - fixed command, no shell
            command, capture_output=True, text=True, timeout=TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ToolResult(False, f"could not run {entrypoint}: {exc}")
    return ToolResult(done.returncode == 0, (done.stdout + done.stderr).strip())


def promtool(*args: str) -> ToolResult:
    """Run promtool with `observability/prometheus` mounted as the working directory."""
    return _run(PROMETHEUS_IMAGE, "/bin/promtool", paths.PROMETHEUS_DIR, PROMETHEUS_MOUNT, [*args])


def amtool(*args: str) -> ToolResult:
    """Run amtool with `observability/alertmanager` mounted as the working directory."""
    return _run(
        ALERTMANAGER_IMAGE, "/bin/amtool", paths.ALERTMANAGER_DIR, ALERTMANAGER_MOUNT, [*args]
    )


def relative_files(base: Path, pattern: str) -> list[str]:
    """POSIX paths of files under `base` matching `pattern`, relative to `base`."""
    return sorted(path.relative_to(base).as_posix() for path in base.glob(pattern))
