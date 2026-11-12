"""Production wiring: builds the concrete adapters behind the agent's ports."""

from __future__ import annotations

import asyncio

import httpx

from agent.adapters.docker_exec import DockerExecutor, DockerSdkRuntime
from agent.adapters.notifier import LogNotifier
from agent.adapters.prometheus import PrometheusMetrics
from agent.adapters.sqlite_store import SqliteStore
from agent.app.api import Components
from agent.app.config import AgentSettings
from agent.app.metrics import AgentMetrics
from agent.app.pipeline import IncidentPipeline
from agent.diagnosis.engine import Diagnoser, RulesDiagnoser
from agent.diagnosis.llm.base import LLMClient
from agent.diagnosis.llm.fake import FakeLLM
from agent.diagnosis.llm.gemini import GeminiClient
from agent.diagnosis.llm_engine import LlmDiagnoser
from agent.evidence.catalog import load_catalog
from agent.evidence.collector import EvidenceCollector
from agent.policy.engine import PolicyEngine
from agent.policy.limits import load_policy
from agent.verify.verifier import Verifier
from libs.core.cmdb import load_cmdb


def build_llm_client(settings: AgentSettings) -> LLMClient | None:
    """The configured LLM provider, or None when Gemini is selected without a key."""
    if settings.llm_provider == "fake":
        fixture = settings.llm_fake_fixture
        return FakeLLM.from_fixture(fixture) if fixture else FakeLLM()
    key = settings.gemini_api_key.get_secret_value()
    if not key:
        return None
    return GeminiClient(
        key,
        model=settings.gemini_model,
        temperature=settings.llm_temperature,
        max_output_tokens=settings.llm_max_output_tokens,
        probe_timeout_s=settings.llm_probe_timeout_s,
    )


async def build_components(settings: AgentSettings, metrics: AgentMetrics) -> Components:
    """Create every adapter. Invalid CMDB, policy or catalog stops startup (fail fast)."""
    cmdb = load_cmdb(settings.cmdb_path)
    policy = load_policy(settings.policy_path)
    catalog = load_catalog()

    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(settings.db_path)
    await asyncio.to_thread(store.migrate)

    http = httpx.AsyncClient(timeout=settings.http_timeout_s)
    prometheus = PrometheusMetrics(http, settings.prometheus_url)
    runtime = DockerSdkRuntime()
    executor = DockerExecutor(
        runtime,
        cmdb,
        project=settings.compose_project,
        versions_file=settings.versions_file,
        action_timeout_s=settings.action_timeout_s,
    )
    collector = EvidenceCollector(
        settings=settings,
        cmdb=cmdb,
        catalog=catalog,
        metrics=prometheus,
        logs=executor,
        deploys=store,
        executor=executor,
    )
    rules = RulesDiagnoser(settings)
    diagnosers: dict[str, Diagnoser] = {"rules": rules}
    llm_client = build_llm_client(settings)
    if llm_client is not None:
        diagnosers["llm"] = LlmDiagnoser(
            settings=settings,
            cmdb=cmdb,
            catalog=catalog,
            client=llm_client,
            collector=collector,
            fallback=rules,
            metrics=metrics,
        )
    elif settings.diagnosis_mode == "llm":
        raise ValueError("DIAGNOSIS_MODE=llm needs GEMINI_API_KEY (or LLM_PROVIDER=fake)")

    pipeline = IncidentPipeline(
        settings=settings,
        cmdb=cmdb,
        store=store,
        collector=collector,
        diagnosers=diagnosers,
        policy=PolicyEngine(policy, cmdb, store),
        executor=executor,
        verifier=Verifier(
            settings=settings, cmdb=cmdb, catalog=catalog, metrics=prometheus, executor=executor
        ),
        notifier=LogNotifier(),
        metrics=metrics,
    )

    async def readiness() -> dict[str, str]:
        failures: dict[str, str] = {}
        try:
            await asyncio.to_thread(store.ping)
        except Exception as exc:  # readiness must report, never raise
            failures["store"] = type(exc).__name__
        if not await prometheus.ready():
            failures["prometheus"] = "unreachable"
        if not await asyncio.to_thread(runtime.ping):
            failures["docker"] = "unreachable"
        return failures

    async def close() -> None:
        await http.aclose()
        await asyncio.to_thread(store.close)

    return Components(store=store, pipeline=pipeline, readiness=readiness, close=close)
