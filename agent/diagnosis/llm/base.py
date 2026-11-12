"""The LLM port: structured generation with explicit failure kinds."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

# Failure kinds, also used as the `kind` label of agent_llm_errors_total.
TIMEOUT = "timeout"
RATE_LIMITED = "rate_limited"
SERVER_ERROR = "server_error"
AUTH = "auth"
BAD_REQUEST = "bad_request"
INVALID_OUTPUT = "invalid_output"
UNAVAILABLE = "unavailable"

RETRYABLE = frozenset({TIMEOUT, RATE_LIMITED, SERVER_ERROR})


class LLMError(Exception):
    """An LLM call failed. `kind` says how; `retryable` says whether trying again can help."""

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(message or kind)
        self.kind = kind
        self.retryable = kind in RETRYABLE


@dataclass(frozen=True)
class LLMResult:
    """A parsed response plus what it cost."""

    parsed: BaseModel
    tokens_in: int
    tokens_out: int
    latency_s: float
    raw_text: str
    model: str
    provider: str


class LLMClient(Protocol):
    """Generates one response that must validate against `schema`."""

    provider: str

    async def generate_structured(
        self, system: str, user: str, schema: type[BaseModel], *, timeout: float
    ) -> LLMResult:
        """Return a validated response or raise `LLMError`."""
        ...
