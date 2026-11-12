"""Notifier adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from services.kit.logging import get_logger


class LogNotifier:
    """Writes incident events to the structured log."""

    async def publish(self, kind: str, payload: Mapping[str, Any]) -> None:
        get_logger().info("incident_event", kind=kind, **dict(payload))
