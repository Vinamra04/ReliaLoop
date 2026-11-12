"""End-to-end checks of the alerting pipeline, and silence management.

`verify_pipeline` answers one question: if something broke right now, would
the page reach the agent? It checks every hop with real requests.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

TIMEOUT_S = 10.0
TEST_ALERT_NAME = "PipelineTest"
TEST_ALERT_LIFETIME_S = 60
DELIVERY_WAIT_S = 45.0
POLL_INTERVAL_S = 1.0
RECEIVED_PAGES = re.compile(r'^agent_alerts_received_total\{severity="page"\} (\S+)$', re.MULTILINE)


@dataclass(frozen=True)
class Endpoints:
    prometheus: str
    alertmanager: str
    agent: str


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _check(name: str, probe: Callable[[], tuple[bool, str]]) -> Check:
    """Run one probe; a transport error is a failed check, never a crash."""
    try:
        ok, detail = probe()
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        return Check(name, False, f"{type(exc).__name__}: {exc}")
    return Check(name, ok, detail)


def received_pages(metrics_text: str) -> float:
    """Value of `agent_alerts_received_total{severity="page"}` in a metrics page."""
    match = RECEIVED_PAGES.search(metrics_text)
    return float(match.group(1)) if match else 0.0


def test_alert(now: datetime) -> dict[str, Any]:
    """A short-lived page-severity alert the agent recognises as a test."""
    return {
        "labels": {
            "alertname": TEST_ALERT_NAME,
            "severity": "page",
            "class": "meta",
            "service": "pipeline-test",
            "relialoop_test": "true",
        },
        "annotations": {"summary": "Synthetic alert sent by verify-pipeline"},
        "startsAt": now.isoformat(),
        "endsAt": (now + timedelta(seconds=TEST_ALERT_LIFETIME_S)).isoformat(),
    }


def verify_pipeline(
    endpoints: Endpoints,
    webhook_token: str,
    client: httpx.Client,
    *,
    sleep: Callable[[float], None] = time.sleep,
    delivery_wait_s: float = DELIVERY_WAIT_S,
) -> list[Check]:
    """Run every pipeline check and return the results in order."""

    def targets() -> tuple[bool, str]:
        data = client.get(f"{endpoints.prometheus}/api/v1/targets").json()["data"]
        down = sorted(
            f"{t['labels'].get('service', '?')}@{t['labels'].get('instance', '?')}"
            for t in data["activeTargets"]
            if t["health"] != "up"
        )
        total = len(data["activeTargets"])
        return (not down and total > 0, f"{total} targets up" if not down else f"down: {down}")

    def rules_healthy() -> tuple[bool, str]:
        groups = client.get(f"{endpoints.prometheus}/api/v1/rules").json()["data"]["groups"]
        rules = [rule for group in groups for rule in group["rules"]]
        broken = sorted(rule["name"] for rule in rules if rule["health"] != "ok")
        return (
            not broken and bool(rules),
            f"{len(rules)} rules healthy" if not broken else f"failing: {broken}",
        )

    def watchdog() -> tuple[bool, str]:
        alerts = client.get(f"{endpoints.alertmanager}/api/v2/alerts").json()
        found = [a for a in alerts if a["labels"].get("alertname") == "Watchdog"]
        if not found:
            return False, "Watchdog is not present in Alertmanager"
        receivers = sorted(r["name"] for r in found[0]["receivers"])
        return receivers == ["null"], f"Watchdog present, receivers={receivers}"

    def auth_rejected() -> tuple[bool, str]:
        body = {"groupKey": "verify-pipeline", "status": "firing", "alerts": []}
        status = client.post(f"{endpoints.agent}/webhook/alertmanager", json=body).status_code
        return status == 401, f"request without token got {status}"

    def auth_accepted() -> tuple[bool, str]:
        now = datetime.now(UTC)
        body = {
            "groupKey": "verify-pipeline",
            "status": "firing",
            "alerts": [{**test_alert(now), "status": "firing", "fingerprint": "verifypipeline"}],
        }
        response = client.post(
            f"{endpoints.agent}/webhook/alertmanager",
            json=body,
            headers={"Authorization": f"Bearer {webhook_token}"},
        )
        return response.status_code == 202, f"request with token got {response.status_code}"

    def routed_to_agent() -> tuple[bool, str]:
        before = received_pages(client.get(f"{endpoints.agent}/metrics").text)
        posted = client.post(
            f"{endpoints.alertmanager}/api/v2/alerts", json=[test_alert(datetime.now(UTC))]
        )
        if posted.status_code != 200:
            return False, f"Alertmanager rejected the test alert: {posted.status_code}"
        waited = 0.0
        while waited < delivery_wait_s:
            sleep(POLL_INTERVAL_S)
            waited += POLL_INTERVAL_S
            if received_pages(client.get(f"{endpoints.agent}/metrics").text) > before:
                return True, f"test alert delivered to the agent after about {waited:.0f}s"
        return False, f"test alert not delivered within {delivery_wait_s:.0f}s"

    return [
        _check("Prometheus targets are up", targets),
        _check("Prometheus rules evaluate without errors", rules_healthy),
        _check("Watchdog reaches Alertmanager and is routed to null", watchdog),
        _check("Agent webhook rejects requests without a token", auth_rejected),
        _check("Agent webhook accepts the bearer token", auth_accepted),
        _check("A page sent to Alertmanager is delivered to the agent", routed_to_agent),
    ]


# --- silences ----------------------------------------------------------------


def silence_payload(
    service: str, minutes: int, comment: str, created_by: str, now: datetime
) -> dict[str, Any]:
    """Alertmanager API v2 silence for every alert of one service."""
    return {
        "matchers": [{"name": "service", "value": service, "isRegex": False, "isEqual": True}],
        "startsAt": now.isoformat(),
        "endsAt": (now + timedelta(minutes=minutes)).isoformat(),
        "createdBy": created_by,
        "comment": comment,
    }


def create_silence(
    client: httpx.Client, alertmanager: str, service: str, minutes: int, comment: str
) -> str:
    """Create a silence and return its id."""
    payload = silence_payload(service, minutes, comment, "relialoop-cli", datetime.now(UTC))
    response = client.post(f"{alertmanager}/api/v2/silences", json=payload)
    response.raise_for_status()
    return str(response.json()["silenceID"])


def list_silences(client: httpx.Client, alertmanager: str) -> list[dict[str, Any]]:
    """Active and pending silences, soonest to end first."""
    response = client.get(f"{alertmanager}/api/v2/silences")
    response.raise_for_status()
    live = [s for s in response.json() if s["status"]["state"] in ("active", "pending")]
    return sorted(live, key=lambda s: str(s["endsAt"]))


def expire_silence(client: httpx.Client, alertmanager: str, silence_id: str) -> None:
    """End a silence now."""
    client.delete(f"{alertmanager}/api/v2/silence/{silence_id}").raise_for_status()
