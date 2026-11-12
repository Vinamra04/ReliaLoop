"""Naming conventions for generated SLO series and alerts.

One place defines the names so the rule generator, the deploy gate, the agent
and the console cannot drift apart.
"""

from __future__ import annotations

ERROR_BUDGET_REMAINING = "slo:error_budget_remaining:ratio"
OBJECTIVE = "slo:objective:ratio"
BURN_ALERT_PREFIX = "SLOBurnRate"

# Routes that never count towards an SLI.
OPERATIONAL_ROUTES_REGEX = "/healthz|/readyz|/metrics|/chaos.*"


def sli_error_ratio(window: str) -> str:
    """Share of bad events over `window`, e.g. `slo:sli_error:ratio_rate5m`."""
    return f"slo:sli_error:ratio_rate{window}"


def sli_total_rate(window: str) -> str:
    """Events per second over `window`; used by the no-traffic guard."""
    return f"slo:sli_total:rate{window}"


def burn_rate(window: str) -> str:
    """Error ratio divided by the error budget, e.g. `slo:burn_rate:ratio_1h`."""
    return f"slo:burn_rate:ratio_{window}"


def burn_alert(tier: str) -> str:
    """Alert name for a burn tier, e.g. `SLOBurnRateFast`."""
    return f"{BURN_ALERT_PREFIX}{tier.capitalize()}"
