"""Multi-window, multi-burn-rate alerting profiles.

`production` is exactly the table from the Google SRE Workbook ("Alerting on
SLOs", multiwindow multi-burn-rate). `demo` keeps the same burn factors but
compresses the windows about 30x so an injected fault is detected in roughly
a minute instead of an hour.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BurnTier:
    """One alert: fires when both windows burn faster than `factor`."""

    tier: str
    factor: float
    long_window: str
    short_window: str
    severity: str
    for_duration: str
    meaning: str


@dataclass(frozen=True)
class Profile:
    name: str
    # Window over which the error budget is accounted. `None` means the
    # spec's own (production) window.
    budget_window: str | None
    tiers: tuple[BurnTier, ...]

    @property
    def windows(self) -> tuple[str, ...]:
        """Every distinct rate window the tiers need, shortest first."""
        seen = {w for tier in self.tiers for w in (tier.long_window, tier.short_window)}
        return tuple(sorted(seen, key=duration_seconds))


_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def duration_seconds(duration: str) -> int:
    """Convert a Prometheus duration such as `5m` to seconds."""
    return int(duration[:-1]) * _UNIT_SECONDS[duration[-1]]


_FAST = "The whole error budget would be gone in a small fraction of the window: act now."
_SLOW = "Sustained burn that still exhausts the budget well before the window ends."
_TICKET = "Budget is draining faster than planned; needs attention during working hours."
_TRICKLE = "Burning at or above the sustainable rate; the budget will not last the window."

PRODUCTION = Profile(
    name="production",
    budget_window=None,
    tiers=(
        BurnTier("fast", 14.4, "1h", "5m", "page", "0s", _FAST),
        BurnTier("slow", 6, "6h", "30m", "page", "0s", _SLOW),
        BurnTier("ticket", 3, "1d", "2h", "ticket", "0s", _TICKET),
        BurnTier("trickle", 1, "3d", "6h", "ticket", "0s", _TRICKLE),
    ),
)

DEMO = Profile(
    name="demo",
    budget_window="1h",
    tiers=(
        BurnTier("fast", 14.4, "2m", "30s", "page", "0s", _FAST),
        BurnTier("slow", 6, "6m", "1m", "page", "10s", _SLOW),
        BurnTier("ticket", 3, "15m", "3m", "ticket", "15s", _TICKET),
        BurnTier("trickle", 1, "30m", "5m", "ticket", "15s", _TRICKLE),
    ),
)

PROFILES = {profile.name: profile for profile in (PRODUCTION, DEMO)}
