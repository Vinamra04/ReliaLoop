"""Real-shape Alertmanager webhook payloads (version 4) for contract tests."""

from __future__ import annotations

from typing import Any

WEBHOOK_TOKEN = "test-webhook-token-0123456789"
APPROVER_TOKEN = "test-approver-token-0123456789"
BEARER = {"Authorization": f"Bearer {WEBHOOK_TOKEN}"}


def alert(
    name: str = "SLOBurnRateFast",
    *,
    status: str = "firing",
    service: str = "gateway",
    slo: str | None = "gateway-availability",
    severity: str = "page",
    fingerprint: str = "a1b2c3d4e5f60718",
    extra_labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    labels = {"alertname": name, "service": service, "severity": severity, "class": "symptom"}
    if slo:
        labels["slo"] = slo
    labels.update(extra_labels or {})
    return {
        "status": status,
        "labels": labels,
        "annotations": {
            "summary": f"{slo or name}: error budget burning",
            "description": "The bad-event ratio exceeded 7.2%.",
            "runbook_url": "http://localhost:8080/runbooks/gateway-availability",
            "dashboard_url": "http://localhost:3000/d/relialoop-slo",
        },
        "startsAt": "2026-11-12T07:30:05.123Z",
        "endsAt": "0001-01-01T00:00:00Z" if status == "firing" else "2026-11-12T07:33:10.000Z",
        "generatorURL": "http://prometheus:9090/graph?g0.expr=...",
        "fingerprint": fingerprint,
    }


def webhook(*alerts: dict[str, Any], status: str | None = None) -> dict[str, Any]:
    items = list(alerts) or [alert()]
    overall = status or ("firing" if any(a["status"] == "firing" for a in items) else "resolved")
    first = items[0]["labels"]
    group = {key: first[key] for key in ("service", "slo") if key in first}
    return {
        "receiver": "agent-pages",
        "status": overall,
        "alerts": items,
        "groupLabels": group,
        "commonLabels": first,
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
        "version": "4",
        "groupKey": '{}/{severity="page"}:{service="gateway", slo="gateway-availability"}',
        "truncatedAlerts": 0,
    }
