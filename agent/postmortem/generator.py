"""Postmortem generator.

Everything factual (timeline, diagnosis, actions, impact) comes from the
incident record and from Prometheus. Only the reflective sections may be
drafted by an LLM, and they are then labelled "AI-drafted — human review
required". Without an LLM those sections are prompts for the reviewer.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from agent.adapters.sqlite_store import SqliteStore
from agent.app.config import AgentSettings
from agent.app.domain import Incident
from agent.diagnosis.llm.base import LLMClient, LLMError
from agent.diagnosis.llm.prompting import POSTMORTEM_PROMPT_VERSION, load_prompt
from agent.diagnosis.schema import LlmPostmortem
from agent.evidence.collector import utc_now
from agent.ports.protocols import MetricsSource, SourceError
from libs.core import naming
from libs.core.cmdb import Cmdb

TEMPLATES = Path(__file__).resolve().parent / "templates"
NOT_AVAILABLE = "not available"
API = f'route!~"{naming.OPERATIONAL_ROUTES_REGEX}"'
BUDGET_WINDOW = "1h"
MAX_ITEMS = 4


def _fmt(ts: datetime | None) -> str | None:
    return ts.strftime("%Y-%m-%d %H:%M:%S UTC") if ts else None


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return NOT_AVAILABLE
    return f"{seconds:.0f} s" if seconds < 120 else f"{seconds / 60:.1f} min"


def _cell(text: Any) -> str:
    """Make text safe for a one-line Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


class PostmortemWriter:
    def __init__(
        self,
        *,
        settings: AgentSettings,
        store: SqliteStore,
        cmdb: Cmdb,
        metrics: MetricsSource,
        llm: LLMClient | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._store = store
        self._cmdb = cmdb
        self._metrics = metrics
        self._llm = llm
        self._clock = clock
        self._env = Environment(
            loader=FileSystemLoader(TEMPLATES),
            undefined=StrictUndefined,
            autoescape=False,  # noqa: S701 - renders Markdown, not HTML
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )

    def path_for(self, incident_id: str) -> Path:
        return self._settings.postmortems_dir / f"{incident_id}.md"

    async def _number(self, query: str) -> float | None:
        try:
            samples = await self._metrics.instant(query)
        except SourceError:
            return None
        return samples[0].value if samples else None

    async def _impact(
        self, incident: Incident, started: datetime, ended: datetime
    ) -> dict[str, str]:
        """Failed requests and error-budget share over the incident window, from Prometheus."""
        seconds = max(1, math.ceil((ended - started).total_seconds()))
        selector = f'http_requests_total{{service="{incident.service}", {API}'
        failed = await self._number(f'sum(increase({selector}, status=~"5.."}}[{seconds}s]))')
        total = await self._number(f"sum(increase({selector}}}[{seconds}s]))")
        slo = incident.slo or next(iter(self._cmdb.services[incident.service].slos), None)
        objective = await self._number(f'{naming.OBJECTIVE}{{slo="{slo}"}}') if slo else None
        window_total = await self._number(f"sum(increase({selector}}}[{BUDGET_WINDOW}]))")
        consumed = NOT_AVAILABLE
        if None not in (failed, objective, window_total) and window_total and objective is not None:
            allowed = (1 - objective) * window_total
            if allowed > 0 and failed is not None:
                consumed = f"{failed / allowed:.0%} of the {BUDGET_WINDOW} budget of {slo}"
        return {
            "duration": _duration((ended - started).total_seconds()),
            "failed_requests": f"{failed:.0f}" if failed is not None else NOT_AVAILABLE,
            "total_requests": f"{total:.0f}" if total is not None else NOT_AVAILABLE,
            "budget_consumed": consumed,
            "note": (
                "Request counts are estimates computed from Prometheus counters over the "
                "incident window (5xx responses at the alerting service). The error budget "
                f"uses the demo profile's {BUDGET_WINDOW} accounting window."
            ),
        }

    async def _narrative(self, facts: dict[str, Any]) -> LlmPostmortem | None:
        """Ask the LLM for the reflective sections; None on any failure."""
        if self._llm is None:
            return None
        try:
            result = await self._llm.generate_structured(
                load_prompt(POSTMORTEM_PROMPT_VERSION),
                json.dumps(facts, default=str),
                LlmPostmortem,
                timeout=self._settings.llm_timeout_s,
            )
        except LLMError:
            return None
        draft = result.parsed
        if not isinstance(draft, LlmPostmortem):
            return None
        return LlmPostmortem(
            summary=draft.summary[:1200],
            what_went_well=[item[:400] for item in draft.what_went_well[:MAX_ITEMS]],
            what_went_badly=[item[:400] for item in draft.what_went_badly[:MAX_ITEMS]],
            action_items=[item[:400] for item in draft.action_items[:MAX_ITEMS]],
        )

    async def write(self, incident: Incident) -> Path:
        """Render the postmortem for an incident and write it to the postmortems directory."""
        db = asyncio.to_thread
        events = await db(self._store.events, incident.id)
        alerts = await db(self._store.alerts, incident.id)
        diagnoses = await db(self._store.diagnoses, incident.id)
        actions = await db(self._store.actions, incident.id)

        now = self._clock()
        alert_started = min(
            (datetime.fromisoformat(a["starts_at"]) for a in alerts), default=incident.opened_at
        )
        ended = incident.closed_at or incident.updated_at or now

        def since(ts: datetime | None) -> str:
            return _duration((ts - alert_started).total_seconds()) if ts else NOT_AVAILABLE

        def first_event(state: str) -> datetime | None:
            for event in events:
                if event["state"] == state and event["kind"] == "state":
                    return datetime.fromisoformat(event["ts"])
            return None

        milestones = [
            {"name": "Alert started firing", "at": _fmt(alert_started), "since": "0 s"},
            {
                "name": "Incident opened (page received)",
                "at": _fmt(incident.opened_at),
                "since": since(incident.opened_at),
            },
        ]
        for name, state in (
            ("Diagnosed", "DIAGNOSED"),
            ("Waiting for approval", "PENDING_APPROVAL"),
            ("Action started", "ACTING"),
            ("Verification started", "VERIFYING"),
            ("Recovery verified", "RESOLVED"),
            ("Escalated to a human", "ESCALATED"),
        ):
            at = first_event(state)
            if at:
                milestones.append({"name": name, "at": _fmt(at), "since": since(at)})

        diagnosis_rows = [
            {
                "category": d["diagnosis"]["category"],
                "root_cause_service": d["diagnosis"]["root_cause_service"],
                "confidence": d["diagnosis"]["confidence"],
                "rationale": d["diagnosis"]["rationale"],
                "evidence": d["diagnosis"]["evidence"],
                "action_type": d["diagnosis"]["action"]["type"],
                "action_target": d["diagnosis"]["action"]["target_service"],
                "action_params": d["diagnosis"]["action"]["params"] or "",
                "mode": d["mode"],
                "provider": d["provider"],
                "model": d["model"],
                "prompt_version": d["prompt_version"],
                "tokens_in": d["tokens_in"],
                "tokens_out": d["tokens_out"],
                "degraded_mode": bool(d["degraded_mode"]),
            }
            for d in diagnoses
        ]
        action_rows = [
            {
                "type": a["type"],
                "target": a["target"],
                "risk": a["risk"],
                "decision": a["decision"],
                "rule": a["rule"],
                "status": a["status"],
                "detail": _cell(a["outcome"].get("detail", ""))[:200],
                "decided_by": a["decided_by"],
            }
            for a in actions
        ]
        verification_events = [
            e["message"]
            for e in events
            if e["state"] in ("RESOLVED", "FAILED_VERIFICATION") and e["kind"] == "state"
        ]
        verification = (
            "\n".join(f"- {message}" for message in verification_events)
            or "No verification ran (nothing was executed)."
        )
        impact = await self._impact(incident, alert_started, ended)

        facts = {
            "incident": incident.model_dump(mode="json"),
            "impact": impact,
            "milestones": milestones,
            "diagnoses": [
                {
                    k: d[k]
                    for k in (
                        "category",
                        "root_cause_service",
                        "confidence",
                        "mode",
                        "action_type",
                        "action_target",
                        "degraded_mode",
                    )
                }
                for d in diagnosis_rows
            ],
            "actions": action_rows,
            "verification": verification_events,
            "validation_failures": [e["data"] for e in events if e["kind"] == "validation"],
        }
        narrative = await self._narrative(facts) if incident.diagnosis_mode == "llm" else None

        text = self._env.get_template("postmortem.md.j2").render(
            incident=incident.model_dump(mode="json"),
            generated_at=_fmt(now),
            narrative=narrative,
            times={
                "alert_started": _fmt(alert_started),
                "opened": _fmt(incident.opened_at),
                "ended": _fmt(ended),
            },
            impact=impact,
            milestones=milestones,
            diagnoses=diagnosis_rows,
            validation_failures=[e["data"] for e in events if e["kind"] == "validation"],
            security_events=[e["message"] for e in events if e["kind"] == "security"],
            actions=action_rows,
            verification=verification,
            timeline=[
                {"ts": e["ts"][11:19], "state": e["state"], "message": _cell(e["message"])}
                for e in events
            ],
        )
        path = self.path_for(incident.id)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_text, text, "utf-8")
        return path
