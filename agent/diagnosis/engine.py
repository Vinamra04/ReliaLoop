"""Diagnosis engines behind one interface, so the pipeline does not care which runs."""

from __future__ import annotations

import time
from typing import Protocol

from agent.app.config import AgentSettings
from agent.app.domain import DiagnosisRecord, EvidenceBundle, ProposedAction
from agent.diagnosis import rules


class Diagnoser(Protocol):
    async def diagnose(
        self, bundle: EvidenceBundle, *, failed_action: ProposedAction | None = None
    ) -> tuple[DiagnosisRecord, EvidenceBundle]:
        """Diagnose from the bundle.

        `failed_action` is set on a re-plan: the action that was tried and did
        not fix the problem. Returns the diagnosis and the bundle actually used
        (an engine may have requested extra evidence).
        """
        ...


class RulesDiagnoser:
    """The deterministic baseline: a decision tree over the evidence."""

    def __init__(self, settings: AgentSettings) -> None:
        self._settings = settings

    async def diagnose(
        self, bundle: EvidenceBundle, *, failed_action: ProposedAction | None = None
    ) -> tuple[DiagnosisRecord, EvidenceBundle]:
        started = time.perf_counter()
        diagnosis = rules.diagnose(bundle, self._settings)
        record = DiagnosisRecord(
            diagnosis=diagnosis,
            mode="rules",
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        return record, bundle
