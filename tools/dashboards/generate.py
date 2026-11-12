"""Render dashboard definitions to JSON files, or check they are up to date."""

from __future__ import annotations

import json
from pathlib import Path

from tools.dashboards.definitions import DASHBOARDS


def render_all() -> dict[str, str]:
    """Return file name -> JSON text for every dashboard."""
    return {
        name: json.dumps(build(), indent=2, sort_keys=True) + "\n"
        for name, build in DASHBOARDS.items()
    }


def write_all(directory: Path) -> list[str]:
    """Write every dashboard; return the names that changed."""
    directory.mkdir(parents=True, exist_ok=True)
    changed = []
    for name, text in render_all().items():
        target = directory / name
        if not target.exists() or target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8", newline="\n")
            changed.append(name)
    return changed


def stale(directory: Path) -> list[str]:
    """Names whose committed JSON differs from what the definitions produce."""
    return [
        name
        for name, text in render_all().items()
        if not (directory / name).exists() or (directory / name).read_text(encoding="utf-8") != text
    ]
