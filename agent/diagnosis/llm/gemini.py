"""Gemini adapter (official `google-genai` SDK, JSON mode with a response schema)."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from pydantic import BaseModel, ValidationError

from agent.diagnosis.llm.base import (
    AUTH,
    BAD_REQUEST,
    INVALID_OUTPUT,
    RATE_LIMITED,
    SERVER_ERROR,
    TIMEOUT,
    UNAVAILABLE,
    LLMError,
    LLMResult,
)

# Plain flash-class text models only: no -lite, -image or -preview variants.
FLASH_MODEL = re.compile(r"^(?:models/)?(gemini-(\d+)(?:\.(\d+))?-flash)$")


def flash_candidates(names: list[str]) -> list[str]:
    """Flash-class model names, newest version first."""
    found: list[tuple[tuple[int, int], str]] = []
    for name in names:
        match = FLASH_MODEL.match(name)
        if match:
            found.append(((int(match.group(2)), int(match.group(3) or 0)), match.group(1)))
    return [name for _, name in sorted(found, reverse=True)]


def classify_status(code: int | None) -> str:
    """Map an HTTP status from the API to a failure kind."""
    if code == 429:
        return RATE_LIMITED
    if code in (401, 403):
        return AUTH
    if code is not None and 500 <= code < 600:
        return SERVER_ERROR
    if code is not None and 400 <= code < 500:
        return BAD_REQUEST
    return UNAVAILABLE


class _Probe(BaseModel):
    ok: bool


class GeminiClient:
    """Calls Gemini. The API key is held by the SDK client and never logged."""

    provider = "gemini"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "",
        temperature: float = 0.0,
        max_output_tokens: int = 4096,
        probe_timeout_s: float = 20.0,
        sdk_client: Any | None = None,
    ) -> None:
        from google import genai
        from google.genai import errors, types

        self._types = types
        self._api_error = errors.APIError
        self._client = sdk_client or genai.Client(api_key=api_key)
        self._model = model
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._probe_timeout_s = probe_timeout_s
        self._select_lock = asyncio.Lock()

    async def _generate(
        self, model: str, system: str, user: str, schema: type[BaseModel], timeout: float
    ) -> LLMResult:
        config = self._types.GenerateContentConfig(
            system_instruction=system,
            temperature=self._temperature,
            response_mime_type="application/json",
            response_schema=schema,
            max_output_tokens=self._max_output_tokens,
        )
        started = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(model=model, contents=user, config=config),
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise LLMError(TIMEOUT, f"no response within {timeout:.0f}s") from exc
        except self._api_error as exc:
            code = getattr(exc, "code", None)
            raise LLMError(classify_status(code), f"API error {code}") from exc
        except Exception as exc:  # network errors and anything the SDK did not wrap
            raise LLMError(UNAVAILABLE, type(exc).__name__) from exc
        latency = time.perf_counter() - started

        text = response.text or ""
        try:
            parsed = schema.model_validate_json(text)
        except ValidationError as exc:
            raise LLMError(INVALID_OUTPUT, "response does not match the schema") from exc
        usage = response.usage_metadata
        return LLMResult(
            parsed=parsed,
            tokens_in=int(getattr(usage, "prompt_token_count", 0) or 0),
            # Reasoning tokens are billed as output, so count them as output.
            tokens_out=int(getattr(usage, "candidates_token_count", 0) or 0)
            + int(getattr(usage, "thoughts_token_count", 0) or 0),
            latency_s=latency,
            raw_text=text,
            model=model,
            provider=self.provider,
        )

    async def select_model(self) -> str:
        """Pinned model, or the newest flash-class model that answers a probe (ADR 0003)."""
        async with self._select_lock:
            if self._model:
                return self._model
            try:
                pager = await asyncio.wait_for(
                    self._client.aio.models.list(), timeout=self._probe_timeout_s
                )
                names = [model.name async for model in pager]
            except TimeoutError as exc:
                raise LLMError(TIMEOUT, "listing models timed out") from exc
            except self._api_error as exc:
                raise LLMError(
                    classify_status(getattr(exc, "code", None)), "listing failed"
                ) from exc
            except Exception as exc:
                raise LLMError(UNAVAILABLE, type(exc).__name__) from exc
            for candidate in flash_candidates(names):
                try:
                    await self._generate(
                        candidate,
                        "Reply with JSON only.",
                        'Return {"ok": true}.',
                        _Probe,
                        self._probe_timeout_s,
                    )
                except LLMError as exc:
                    if exc.kind == AUTH:
                        raise
                    continue
                self._model = candidate
                return candidate
            raise LLMError(UNAVAILABLE, "no flash-class model answered the probe")

    async def generate_structured(
        self, system: str, user: str, schema: type[BaseModel], *, timeout: float
    ) -> LLMResult:
        model = await self.select_model()
        return await self._generate(model, system, user, schema, timeout)
