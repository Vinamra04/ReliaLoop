"""Pre-commit secret scan.

Why: the Gemini key and service tokens must never reach a commit. Generic hooks
catch private keys; this one catches (1) common API-key shapes and (2) the
exact secret values currently in `.env`, whatever their format. The matched
text is never printed, only the file, line number and pattern name.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERNS: dict[str, re.Pattern[str]] = {
    "google-api-key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "google-oauth-token": re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}"),
    "openai-style-key": re.compile(r"sk-[A-Za-z0-9]{32,}"),
    "github-token": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    "aws-access-key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "assigned-gemini-key": re.compile(r"GEMINI_API_KEY\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{20,}"),
}

MAX_BYTES = 2_000_000
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
SECRET_NAME = re.compile(r"(TOKEN|KEY|PASSWORD|SECRET)", re.IGNORECASE)
MIN_SECRET_LENGTH = 16


def env_secrets(env_file: Path = ENV_FILE) -> dict[str, str]:
    """Secret values from `.env`, keyed by variable name (empty if the file is absent)."""
    if not env_file.exists():
        return {}
    secrets: dict[str, str] = {}
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip()
        if SECRET_NAME.search(name) and len(value) >= MIN_SECRET_LENGTH:
            secrets[name.strip()] = value
    return secrets


def scan_text(text: str, secrets: dict[str, str] | None = None) -> list[tuple[int, str]]:
    """Return (line_number, pattern_name) for every suspicious line."""
    hits: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        hits.extend((number, name) for name, pattern in PATTERNS.items() if pattern.search(line))
        hits.extend(
            (number, f"value-of-{name}") for name, value in (secrets or {}).items() if value in line
        )
    return hits


def scan_file(path: Path, secrets: dict[str, str] | None = None) -> list[tuple[int, str]]:
    """Scan one file; unreadable, binary or very large files are skipped."""
    try:
        if path.stat().st_size > MAX_BYTES:
            return []
        return scan_text(path.read_text(encoding="utf-8"), secrets)
    except (OSError, UnicodeDecodeError):
        return []


def main(argv: list[str]) -> int:
    """Exit 1 if any file given on the command line looks like it holds a secret."""
    secrets = env_secrets()
    found = False
    for name in argv:
        for line_number, pattern_name in scan_file(Path(name), secrets):
            found = True
            sys.stderr.write(f"possible secret ({pattern_name}) at {name}:{line_number}\n")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
