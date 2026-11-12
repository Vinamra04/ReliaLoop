"""Prompt loading and rendering.

Prompts are versioned files; the version is recorded on every diagnosis so a
result can always be traced to the exact instructions that produced it. The
user prompt is assembled from the evidence bundle only: nothing free-form
from the outside world reaches the model except inside the `<logs>` block,
which the system prompt declares untrusted.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from agent.app.domain import EvidenceBundle, EvidenceItem, ProposedAction
from agent.evidence.catalog import Catalog
from libs.core.cmdb import Cmdb

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
DIAGNOSIS_PROMPT_VERSION = "diagnosis-v1"
POSTMORTEM_PROMPT_VERSION = "postmortem-v1"


@lru_cache(maxsize=8)
def load_prompt(version: str) -> str:
    """Read a prompt file by version, e.g. `diagnosis-v1` -> `diagnosis_v1.md`."""
    return (PROMPTS_DIR / f"{version.replace('-', '_')}.md").read_text(encoding="utf-8")


def neutralise(text: str) -> str:
    """Make log text unable to close the `<logs>` block or open another tag."""
    return text.replace("<", "(").replace(">", ")")


def _value(item: EvidenceItem) -> str:
    if item.value is None:
        return "no data"
    if isinstance(item.value, float):
        return f"{item.value:.4g}"
    return str(item.value)


def _evidence_table(bundle: EvidenceBundle) -> str:
    rows = ["id | value | unit | window | source"]
    rows.extend(
        f"{item.id} | {_value(item)} | {item.unit or '-'} | {item.window or '-'} | {item.source}"
        for item in bundle.items
    )
    return "\n".join(rows)


def _graph(bundle: EvidenceBundle, cmdb: Cmdb) -> str:
    lines = []
    for name in cmdb.dependency_chain(bundle.alerting_service):
        service = cmdb.services[name]
        deps = ", ".join(service.depends_on) or "nothing"
        if service.managed:
            lines.append(
                f"- {name}: depends on {deps}; managed; allowed actions: "
                f"{', '.join(service.allowed_actions) or 'none'}; "
                f"replicas {service.limits.min_replicas}-{service.limits.max_replicas}"
            )
        else:
            lines.append(f"- {name}: depends on {deps}; NOT managed (never a valid action target)")
    return "\n".join(lines)


def _deploys(bundle: EvidenceBundle) -> str:
    if not bundle.deploys:
        return "none recorded"
    return "\n".join(
        f"- {event.ts.isoformat(timespec='seconds')} {event.service}: "
        f"{event.previous_version or '?'} -> {event.version} ({event.reason}, {event.status})"
        for event in bundle.deploys
    )


def _logs(bundle: EvidenceBundle) -> str:
    blocks = []
    for summary in bundle.logs:
        lines = [f"[{summary.service}] {summary.lines_seen} lines seen"]
        lines.extend(
            f"  error x{count}: {neutralise(template)}"
            for template, count in summary.error_templates
        )
        lines.extend(f"  {neutralise(line)}" for line in summary.tail)
        blocks.append("\n".join(lines))
    return "\n".join(blocks) or "no logs collected"


def render_user_prompt(
    bundle: EvidenceBundle,
    cmdb: Cmdb,
    catalog: Catalog,
    *,
    failed_action: ProposedAction | None,
    can_request_more: bool,
) -> str:
    """Render the evidence bundle as the user message."""
    parts = [
        "<incident>",
        f"alerting_service: {bundle.alerting_service}",
        f"slo: {bundle.slo or 'none (cause alert)'}",
        f"evidence_collected_at: {bundle.collected_at.isoformat(timespec='seconds')}",
        f"incomplete_sources: {', '.join(bundle.incomplete) or 'none'}",
        "</incident>",
        "",
        "<dependency_graph>",
        _graph(bundle, cmdb),
        "</dependency_graph>",
        "",
        "<evidence>",
        _evidence_table(bundle),
        "</evidence>",
        "",
        "<deploy_history>",
        _deploys(bundle),
        "</deploy_history>",
        "",
        "<logs>",
        _logs(bundle),
        "</logs>",
    ]
    if failed_action is not None:
        parts += [
            "",
            "<previous_attempt>",
            f"{failed_action.type.value} on {failed_action.target_service} was executed and did "
            "NOT fix the problem. Do not propose it again; reconsider the diagnosis.",
            "</previous_attempt>",
        ]
    if can_request_more:
        present = {item.name for item in bundle.items}
        extra = [name for name in catalog.optional_names() if name not in present]
        parts += [
            "",
            "<optional_evidence>",
            "You may request these by name in need_more_evidence:",
            *(f"- {name}: {catalog.get(name).description}" for name in extra),
            "</optional_evidence>",
        ]
    else:
        parts += ["", "No further evidence can be requested. Leave need_more_evidence empty."]
    return "\n".join(parts)
