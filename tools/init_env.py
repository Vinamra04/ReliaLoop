"""Create `.env`, service tokens and runtime directories.

Why a dedicated module: `init` must be idempotent and must never overwrite a
value the user already has (for example a pasted Gemini key), so the merge
logic is kept pure and unit-tested apart from the filesystem writes.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

GENERATE_MARKER = "__GENERATE__"
TOKEN_BYTES = 32


@dataclass(frozen=True)
class InitResult:
    """What `init` did, reported without revealing any secret value."""

    env_created: bool
    generated_keys: tuple[str, ...]
    added_keys: tuple[str, ...]
    webhook_file_written: bool


def new_token() -> str:
    """Return a URL-safe random token suitable for bearer auth."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def parse_env(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines, ignoring comments and blank lines."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def merge_env(
    example_text: str,
    existing_text: str | None,
    token_factory: Callable[[], str] = new_token,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Render `.env` from the example, keeping every value that already exists.

    Returns the new text, the keys that received a freshly generated token and
    the keys that were newly added from the example.
    """
    existing = parse_env(existing_text) if existing_text is not None else {}
    generated: list[str] = []
    added: list[str] = []
    lines: list[str] = []
    seen: set[str] = set()
    for raw in example_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            lines.append(raw)
            continue
        key, _, default = stripped.partition("=")
        key = key.strip()
        seen.add(key)
        current = existing.get(key)
        if current is not None and current != GENERATE_MARKER:
            lines.append(f"{key}={current}")
            continue
        if existing_text is not None and current is None:
            added.append(key)
        if default.strip() == GENERATE_MARKER:
            lines.append(f"{key}={token_factory()}")
            generated.append(key)
        else:
            lines.append(f"{key}={default.strip()}")
    extras = [key for key in existing if key not in seen]
    if extras:
        lines.append("")
        lines.append("# --- Local additions (not in .env.example) ---")
        lines.extend(f"{key}={existing[key]}" for key in extras)
    return "\n".join(lines) + "\n", tuple(generated), tuple(added)


def run_init(
    env_example: Path,
    env_file: Path,
    webhook_token_file: Path,
    runtime_dirs: tuple[Path, ...],
) -> InitResult:
    """Write `.env`, the Alertmanager credentials file and runtime directories."""
    existing_text = env_file.read_text(encoding="utf-8") if env_file.exists() else None
    text, generated, added = merge_env(env_example.read_text(encoding="utf-8"), existing_text)
    env_file.write_text(text, encoding="utf-8", newline="\n")

    webhook_token = parse_env(text)["WEBHOOK_TOKEN"]
    webhook_token_file.parent.mkdir(parents=True, exist_ok=True)
    previous = (
        webhook_token_file.read_text(encoding="utf-8") if webhook_token_file.exists() else None
    )
    webhook_written = previous != webhook_token
    if webhook_written:
        # No trailing newline: Alertmanager sends the file content verbatim.
        webhook_token_file.write_text(webhook_token, encoding="utf-8", newline="\n")

    for directory in runtime_dirs:
        directory.mkdir(parents=True, exist_ok=True)

    return InitResult(
        env_created=existing_text is None,
        generated_keys=generated,
        added_keys=added,
        webhook_file_written=webhook_written,
    )
