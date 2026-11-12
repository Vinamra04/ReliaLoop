"""LLM diagnosis: the agentic mode.

A bounded loop: the model sees the standard evidence bundle, may ask for
optional catalog entries by name (never free-form queries), and must return a
diagnosis matching the schema. Every way this can go wrong (timeout, rate
limit, malformed output, provider outage) ends in the rule-based engine, with
the incident flagged `degraded_mode`. The model's answer is a proposal only:
the validator and the policy engine decide what happens.
"""

from __future__ import annotations

import time

from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from agent.app.config import AgentSettings
from agent.app.domain import DiagnosisRecord, EvidenceBundle, ProposedAction
from agent.app.metrics import AgentMetrics
from agent.diagnosis.engine import Diagnoser
from agent.diagnosis.llm.base import UNAVAILABLE, LLMClient, LLMError, LLMResult
from agent.diagnosis.llm.prompting import (
    DIAGNOSIS_PROMPT_VERSION,
    load_prompt,
    render_user_prompt,
)
from agent.diagnosis.schema import LlmDiagnosis, to_domain
from agent.evidence.catalog import Catalog
from agent.evidence.collector import EvidenceCollector
from agent.evidence.redact import redact
from libs.core.cmdb import Cmdb
from services.kit.logging import get_logger

RAW_OUTPUT_LIMIT = 2000
RETRY_MAX_WAIT_S = 8.0


class _BudgetExhaustedError(Exception):
    """The per-incident cap on LLM calls was reached."""


class LlmDiagnoser:
    def __init__(
        self,
        *,
        settings: AgentSettings,
        cmdb: Cmdb,
        catalog: Catalog,
        client: LLMClient,
        collector: EvidenceCollector,
        fallback: Diagnoser,
        metrics: AgentMetrics,
        retry_wait_s: float = 0.5,
    ) -> None:
        self._settings = settings
        self._cmdb = cmdb
        self._catalog = catalog
        self._client = client
        self._collector = collector
        self._fallback = fallback
        self._metrics = metrics
        self._retry_wait_s = retry_wait_s

    async def _call(self, user: str, budget: list[int]) -> LLMResult:
        """One logical call with bounded retries. `budget[0]` counts API attempts left."""

        async def attempt() -> LLMResult:
            if budget[0] <= 0:
                raise _BudgetExhaustedError
            budget[0] -= 1
            provider = self._client.provider
            try:
                result = await self._client.generate_structured(
                    load_prompt(DIAGNOSIS_PROMPT_VERSION),
                    user,
                    LlmDiagnosis,
                    timeout=self._settings.llm_timeout_s,
                )
            except LLMError as exc:
                self._metrics.llm_calls.labels(provider=provider, outcome="error").inc()
                self._metrics.llm_errors.labels(kind=exc.kind).inc()
                raise
            self._metrics.llm_calls.labels(provider=provider, outcome="ok").inc()
            self._metrics.llm_tokens.labels(direction="in").inc(result.tokens_in)
            self._metrics.llm_tokens.labels(direction="out").inc(result.tokens_out)
            self._metrics.llm_latency.observe(result.latency_s)
            return result

        async for retry in AsyncRetrying(
            stop=stop_after_attempt(self._settings.llm_max_attempts),
            wait=wait_random_exponential(multiplier=self._retry_wait_s, max=RETRY_MAX_WAIT_S),
            retry=retry_if_exception(lambda exc: isinstance(exc, LLMError) and exc.retryable),
            reraise=True,
        ):
            with retry:
                return await attempt()
        raise LLMError(UNAVAILABLE, "retry loop ended without a result")

    async def _degraded(
        self, bundle: EvidenceBundle, failed_action: ProposedAction | None, reason: str
    ) -> tuple[DiagnosisRecord, EvidenceBundle]:
        """Fall back to the rule-based engine and say so on the record."""
        get_logger().warning("llm_fallback_to_rules", reason=reason)
        record, used = await self._fallback.diagnose(bundle, failed_action=failed_action)
        return record.model_copy(update={"degraded_mode": True, "fallback_reason": reason}), used

    async def diagnose(
        self, bundle: EvidenceBundle, *, failed_action: ProposedAction | None = None
    ) -> tuple[DiagnosisRecord, EvidenceBundle]:
        budget = [self._settings.max_llm_calls_per_incident]
        rounds = 0
        tokens_in = tokens_out = 0
        started = time.perf_counter()
        while True:
            can_request_more = rounds < self._settings.max_evidence_rounds and budget[0] > 1
            user = render_user_prompt(
                bundle,
                self._cmdb,
                self._catalog,
                failed_action=failed_action,
                can_request_more=can_request_more,
            )
            try:
                result = await self._call(user, budget)
            except LLMError as exc:
                return await self._degraded(bundle, failed_action, f"llm_error:{exc.kind}")
            except _BudgetExhaustedError:
                return await self._degraded(bundle, failed_action, "llm_call_budget_exhausted")
            tokens_in += result.tokens_in
            tokens_out += result.tokens_out
            raw = result.parsed
            if not isinstance(raw, LlmDiagnosis):
                return await self._degraded(bundle, failed_action, "llm_error:invalid_output")

            present = {item.name for item in bundle.items}
            wanted = [
                name
                for name in dict.fromkeys(raw.need_more_evidence)
                if name in self._catalog.queries and name not in present
            ]
            if wanted and can_request_more:
                bundle = await self._collector.collect_extra(bundle, wanted)
                rounds += 1
                continue

            diagnosis = to_domain(raw).model_copy(update={"need_more_evidence": ()})
            record = DiagnosisRecord(
                diagnosis=diagnosis,
                mode="llm",
                provider=result.provider,
                model=result.model,
                prompt_version=DIAGNOSIS_PROMPT_VERSION,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                llm_calls=self._settings.max_llm_calls_per_incident - budget[0],
                evidence_rounds=rounds,
                raw_output=redact(result.raw_text)[:RAW_OUTPUT_LIMIT],
            )
            return record, bundle
