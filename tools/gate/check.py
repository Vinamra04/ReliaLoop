"""Read error budgets from Prometheus and apply the gate decision."""

from __future__ import annotations

import math

import httpx

from libs.core import naming
from libs.core.gate import GateDecision, decide

DEFAULT_MIN_BUDGET = 0.25


def fetch_budgets(
    client: httpx.Client, prometheus_url: str, service: str, slos: list[str]
) -> dict[str, float | None]:
    """Remaining budget per expected SLO; None for an SLO with no (finite) value.

    Raises `httpx.HTTPError` or `ValueError` when Prometheus cannot answer.
    """
    response = client.get(
        f"{prometheus_url.rstrip('/')}/api/v1/query",
        params={"query": f'{naming.ERROR_BUDGET_REMAINING}{{service="{service}"}}'},
    )
    response.raise_for_status()
    body = response.json()
    if body.get("status") != "success":
        raise ValueError(f"prometheus query failed: {body.get('error', 'unknown error')}")
    found: dict[str, float] = {}
    for series in body["data"]["result"]:
        value = float(series["value"][1])
        if math.isfinite(value):
            found[series["metric"].get("slo", "")] = value
    return {slo: found.get(slo) for slo in slos}


def check_gate(
    client: httpx.Client,
    prometheus_url: str,
    service: str,
    slos: list[str],
    *,
    min_budget: float = DEFAULT_MIN_BUDGET,
    override_reason: str | None = None,
    fail_open: bool = False,
) -> GateDecision:
    """Gate decision for deploying `service`; never raises."""
    try:
        budgets = fetch_budgets(client, prometheus_url, service, slos)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return decide(
            {},
            min_budget=min_budget,
            error=f"Prometheus is unreachable ({type(exc).__name__})",
            override_reason=override_reason,
            fail_open=fail_open,
        )
    return decide(
        budgets, min_budget=min_budget, override_reason=override_reason, fail_open=fail_open
    )
