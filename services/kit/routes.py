"""Route-template helpers shared by logging, metrics and fault injection."""

from __future__ import annotations

from starlette.types import Scope

UNMATCHED_ROUTE = "__unmatched__"

# Operational routes: excluded from SLIs (filtered by label in the rules),
# never fault-injected, and logged at debug level to keep logs readable.
QUIET_ROUTES = frozenset({"/healthz", "/readyz", "/metrics"})
CHAOS_PREFIX = "/chaos"


def route_template(scope: Scope) -> str:
    """Return the matched route template (`/api/orders/{order_id}`), never the raw path.

    Raw paths would create one time series per order id; the template bounds
    label cardinality. Requests that match no route share one label value.
    """
    path = getattr(scope.get("route"), "path", None)
    return path if isinstance(path, str) else UNMATCHED_ROUTE


def is_operational(route: str) -> bool:
    """True for health, metrics and chaos routes."""
    return route in QUIET_ROUTES or route.startswith(CHAOS_PREFIX)
