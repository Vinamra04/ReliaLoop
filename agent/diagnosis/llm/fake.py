"""Scripted LLM for tests and CI: deterministic, offline, free."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from agent.diagnosis.llm.base import INVALID_OUTPUT, LLMError, LLMResult

# Returned when the script is exhausted: the safe answer is "I do not know".
DEFAULT_RESPONSE: dict[str, Any] = {
    "category": "UNKNOWN",
    "root_cause_service": "unknown",
    "confidence": 0.0,
    "insufficient_evidence": True,
    "evidence": [],
    "need_more_evidence": [],
    "action": {"type": "escalate", "target_service": "unknown"},
    "rationale": "Fake LLM: no scripted response available.",
}


class FakeLLM:
    """Returns prepared responses in order and records every prompt it was given.

    A scripted item may be a dict (validated against the requested schema, so
    an invalid dict behaves like invalid model output), raw text, or an
    exception to raise.
    """

    provider = "fake"

    def __init__(self, responses: list[Any] | None = None, model: str = "fake-1") -> None:
        self.responses = list(responses or [])
        self.model = model
        self.calls: list[tuple[str, str]] = []

    @classmethod
    def from_fixture(cls, path: Path) -> FakeLLM:
        return cls(json.loads(path.read_text(encoding="utf-8")))

    async def generate_structured(
        self, system: str, user: str, schema: type[BaseModel], *, timeout: float
    ) -> LLMResult:
        self.calls.append((system, user))
        item = self.responses.pop(0) if self.responses else DEFAULT_RESPONSE
        if isinstance(item, Exception):
            raise item
        raw = item if isinstance(item, str) else json.dumps(item)
        try:
            parsed = schema.model_validate_json(raw)
        except ValidationError as exc:
            raise LLMError(INVALID_OUTPUT, f"response does not match the schema: {exc}") from exc
        return LLMResult(
            parsed=parsed,
            tokens_in=len(system + user) // 4,
            tokens_out=len(raw) // 4,
            latency_s=0.0,
            raw_text=raw,
            model=self.model,
            provider=self.provider,
        )
