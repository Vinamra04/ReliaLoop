"""Redaction of secrets and personal data from untrusted text.

Applied to every log line before it is stored or shown to an LLM. Order
matters: specific patterns run before the generic long-token ones.
"""

from __future__ import annotations

import re

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{8,}=*")),
    ("api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}")),
    ("api_key", re.compile(r"\b(?:sk|pk|ghp|gho|xox[abp])[-_][A-Za-z0-9_\-]{16,}")),
    (
        "secret",
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|credential)s?"
            r"(\"?\s*[:=]\s*\"?)[^\s\"',;}]{4,}"
        ),
    ),
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("hex", re.compile(r"\b[0-9a-fA-F]{40,}\b")),
    ("token", re.compile(r"\b[A-Za-z0-9+/_\-]{40,}={0,2}")),
)


def redact(text: str) -> str:
    """Replace anything that looks like a secret or an email with a placeholder."""
    for kind, pattern in _PATTERNS:
        if kind == "secret":
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED:secret]", text)
        else:
            text = pattern.sub(f"[REDACTED:{kind}]", text)
    return text
