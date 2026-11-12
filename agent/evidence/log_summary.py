"""Turn raw log lines into a small, redacted summary.

Logs are attacker-influenced text, so nothing here interprets them: lines are
parsed, grouped by message template, counted, redacted and capped in size.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from agent.app.domain import LogSummary
from agent.evidence.redact import redact

ERROR_LEVELS = frozenset({"error", "critical", "fatal", "exception"})
MAX_LINE_CHARS = 300
TOP_TEMPLATES = 5
# Fields worth keeping in the rendered tail, in this order.
TAIL_FIELDS = ("status", "route", "kind", "upstream_status", "error", "version", "duration_ms")

# Text that addresses an AI rather than describing an event. Matching lines are
# still passed on as data; the count just makes the attempt visible.
INSTRUCTION_LIKE = re.compile(
    r"(?i)\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all)\b"
    r".{0,40}\b(instruction|prompt|rule|direction)s?\b"
    r"|\byou are now\b|\bsystem prompt\b|\bas an ai\b|\bnew instructions?\b"
)

_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_HEX = re.compile(r"\b[0-9a-fA-F]{12,}\b")
# Not preceded by a letter, so identifiers such as `v1` or `http2` stay intact.
_NUMBER = re.compile(r"(?<![A-Za-z_])\d+(?:\.\d+)?")


def template_of(message: str) -> str:
    """Collapse variable parts so repeated messages group together."""
    collapsed = _UUID.sub("<id>", message)
    collapsed = _HEX.sub("<id>", collapsed)
    collapsed = _NUMBER.sub("<n>", collapsed)
    return collapsed[:MAX_LINE_CHARS]


def _parse(line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _is_error(record: dict[str, Any]) -> bool:
    level = str(record.get("level", "")).lower()
    status = record.get("status")
    return level in ERROR_LEVELS or (isinstance(status, int) and status >= 500)


def _render(record: dict[str, Any]) -> str:
    parts = [
        str(record.get("ts", ""))[:19],
        str(record.get("level", "")),
        str(record.get("event", "")),
    ]
    parts.extend(f"{key}={record[key]}" for key in TAIL_FIELDS if key in record)
    return " ".join(part for part in parts if part)[:MAX_LINE_CHARS]


def summarize_logs(
    service: str, lines: list[str], *, max_bytes: int, tail_lines: int
) -> LogSummary:
    """Summarise log lines: top error templates with counts, plus a short tail."""
    templates: Counter[str] = Counter()
    rendered: list[str] = []
    suspicious = sum(1 for line in lines if INSTRUCTION_LIKE.search(line))
    for line in lines:
        record = _parse(line)
        if record is None:
            raw = line.strip()
            if raw:
                rendered.append(redact(raw[:MAX_LINE_CHARS]))
            continue
        # Routine request lines are noise; keep failures and everything else.
        if record.get("event") == "request" and not _is_error(record):
            continue
        if _is_error(record):
            templates[redact(template_of(str(record.get("event", ""))))] += 1
        rendered.append(redact(_render(record)))

    top = tuple(templates.most_common(TOP_TEMPLATES))
    budget = max_bytes - sum(len(template.encode()) + 8 for template, _ in top)
    tail: list[str] = []
    truncated = len(rendered) > tail_lines
    for entry in reversed(rendered[-tail_lines:]):
        size = len(entry.encode()) + 1
        if size > budget:
            truncated = True
            break
        budget -= size
        tail.append(entry)
    return LogSummary(
        service=service,
        lines_seen=len(lines),
        error_templates=top,
        tail=tuple(reversed(tail)),
        truncated=truncated,
        suspicious_lines=suspicious,
    )
