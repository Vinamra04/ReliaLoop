"""Error-budget deploy gate: the decision, as a pure function.

Shared by the CLI and the agent's `/admin/deploy`, so both apply exactly the
same rule: do not ship changes to a service that has spent its error budget.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

EXIT_ALLOW = 0
EXIT_BLOCKED = 2
EXIT_ERROR = 3


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    exit_code: int
    reason: str
    budgets: Mapping[str, float | None] = field(default_factory=dict)
    overridden: bool = False
    failed_open: bool = False


def decide(
    budgets: Mapping[str, float | None],
    *,
    min_budget: float,
    error: str | None = None,
    override_reason: str | None = None,
    fail_open: bool = False,
) -> GateDecision:
    """Decide whether a deploy may proceed.

    `budgets` maps each SLO of the service to its remaining error budget
    (1 = untouched, 0 = spent), or None when the value is missing. `error` is
    set when the budgets could not be read at all.

    - Unknown state (error or a missing budget) fails **closed**: exit 3.
      Only `fail_open` lets the deploy through, and the decision says so.
    - A budget below `min_budget` blocks: exit 2. An `override_reason` lets it
      through and is recorded.
    - Otherwise the deploy is allowed: exit 0.
    """
    missing = sorted(slo for slo, value in budgets.items() if value is None)
    unknown = error or (f"no budget data for {', '.join(missing)}" if missing else None)
    if not unknown and not budgets:
        unknown = "the service has no SLOs to check"
    if unknown:
        if fail_open:
            return GateDecision(
                True,
                EXIT_ALLOW,
                f"FAIL-OPEN: allowed although {unknown}",
                budgets,
                failed_open=True,
            )
        return GateDecision(False, EXIT_ERROR, f"blocked (fail closed): {unknown}", budgets)

    low = {slo: value for slo, value in budgets.items() if value is not None and value < min_budget}
    if low:
        worst = min(low, key=lambda slo: low[slo])
        detail = f"{worst} has {low[worst]:.0%} of its error budget left (minimum {min_budget:.0%})"
        if override_reason:
            return GateDecision(
                True,
                EXIT_ALLOW,
                f"OVERRIDE: {detail}; reason: {override_reason}",
                budgets,
                overridden=True,
            )
        return GateDecision(False, EXIT_BLOCKED, f"blocked: {detail}", budgets)
    return GateDecision(
        True, EXIT_ALLOW, f"allowed: every SLO has at least {min_budget:.0%} left", budgets
    )
