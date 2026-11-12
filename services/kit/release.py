"""Simulated bad releases, selected by the RELEASE_PROFILE baked into an image.

`good` does nothing. `bad` fails a share of requests and slows the rest from
the first request (a regression shipped in a deploy). `crash` exits shortly
after start so the container crash-loops under its restart policy.
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request

from services.kit.logging import get_logger
from services.kit.routes import is_operational, route_template
from services.kit.settings import ServiceSettings


def bad_release_gate(
    settings: ServiceSettings, rng: random.Random | None = None
) -> Callable[[Request], Any]:
    """Dependency reproducing the `bad` profile's regression."""
    source = rng or random.Random()  # noqa: S311 - fault sampling, not security

    async def gate(request: Request) -> None:
        if is_operational(route_template(request.scope)):
            return
        await asyncio.sleep(settings.bad_release_latency_ms / 1000)
        if source.random() < settings.bad_release_error_rate:
            get_logger().error(
                "unhandled exception in request handler: KeyError('pricing_rule')",
                kind="request_failed",
                status=500,
                version=settings.release_version,
            )
            raise HTTPException(status_code=500, detail="internal server error")

    return gate


def schedule_crash(settings: ServiceSettings) -> None:
    """Exit with code 1 after the configured delay (the `crash` profile)."""
    get_logger().error(
        "fatal: failed to initialise worker pool, exiting",
        kind="startup_failure",
        version=settings.release_version,
        exit_in_s=settings.crash_release_after_s,
    )
    asyncio.get_running_loop().call_later(settings.crash_release_after_s, os._exit, 1)
