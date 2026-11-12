from datetime import UTC, datetime

import httpx
import pytest
import respx

from tools import pipeline

ENDPOINTS = pipeline.Endpoints("http://prom", "http://am", "http://agent")
TOKEN = "test-webhook-token-0123456789"


def metrics(count: int) -> str:
    return (
        "# HELP agent_alerts_received_total x\n"
        f'agent_alerts_received_total{{severity="page"}} {count}.0\n'
        'agent_alerts_received_total{severity="ticket"} 7.0\n'
    )


def healthy_routes(router: respx.MockRouter, *, delivered: bool = True) -> None:
    router.get("http://prom/api/v1/targets").respond(
        json={
            "data": {
                "activeTargets": [
                    {"health": "up", "labels": {"service": "gateway", "instance": "g1"}},
                    {"health": "up", "labels": {"service": "orders", "instance": "o1"}},
                ]
            }
        }
    )
    router.get("http://prom/api/v1/rules").respond(
        json={"data": {"groups": [{"rules": [{"name": "Watchdog", "health": "ok"}]}]}}
    )
    router.get("http://am/api/v2/alerts").respond(
        json=[{"labels": {"alertname": "Watchdog"}, "receivers": [{"name": "null"}]}]
    )

    def webhook(request: httpx.Request) -> httpx.Response:
        authorised = request.headers.get("authorization") == f"Bearer {TOKEN}"
        return httpx.Response(202 if authorised else 401, json={})

    router.post("http://agent/webhook/alertmanager").mock(side_effect=webhook)
    router.post("http://am/api/v2/alerts").respond(200, json={})
    counts = iter([3, 3, 4] if delivered else [3] * 100)
    router.get("http://agent/metrics").mock(
        side_effect=lambda request: httpx.Response(200, text=metrics(next(counts)))
    )


def run(**kwargs) -> list[pipeline.Check]:
    with httpx.Client() as client:
        return pipeline.verify_pipeline(ENDPOINTS, TOKEN, client, sleep=lambda _: None, **kwargs)


def test_received_pages_parses_the_counter():
    assert pipeline.received_pages(metrics(12)) == 12
    assert pipeline.received_pages("nothing relevant") == 0


@respx.mock
def test_all_checks_pass_on_a_healthy_pipeline(respx_mock: respx.MockRouter):
    healthy_routes(respx_mock)
    checks = run()
    assert [check.ok for check in checks] == [True] * 6
    assert "delivered to the agent" in checks[-1].detail


@respx.mock
def test_test_alert_is_marked_so_the_agent_ignores_it(respx_mock: respx.MockRouter):
    healthy_routes(respx_mock)
    run()
    posted = next(
        call.request
        for call in respx_mock.calls
        if str(call.request.url) == "http://am/api/v2/alerts" and call.request.method == "POST"
    )
    assert b'"relialoop_test":"true"' in posted.content.replace(b" ", b"")
    assert b'"severity":"page"' in posted.content.replace(b" ", b"")


@respx.mock
def test_undelivered_test_alert_fails_the_last_check(respx_mock: respx.MockRouter):
    healthy_routes(respx_mock, delivered=False)
    checks = run(delivery_wait_s=3)
    assert [check.ok for check in checks] == [True, True, True, True, True, False]
    assert "not delivered within 3s" in checks[-1].detail


@respx.mock
def test_down_target_missing_watchdog_and_broken_rule_are_reported(respx_mock: respx.MockRouter):
    healthy_routes(respx_mock)
    respx_mock.get("http://prom/api/v1/targets").respond(
        json={
            "data": {
                "activeTargets": [
                    {"health": "down", "labels": {"service": "orders", "instance": "o1"}}
                ]
            }
        }
    )
    respx_mock.get("http://prom/api/v1/rules").respond(
        json={"data": {"groups": [{"rules": [{"name": "Broken", "health": "err"}]}]}}
    )
    respx_mock.get("http://am/api/v2/alerts").respond(json=[])
    checks = run()
    assert [check.ok for check in checks[:3]] == [False, False, False]
    assert "orders@o1" in checks[0].detail
    assert "Broken" in checks[1].detail
    assert "not present" in checks[2].detail


@respx.mock
def test_wrong_token_and_unreachable_services_fail_cleanly(respx_mock: respx.MockRouter):
    healthy_routes(respx_mock)
    respx_mock.get("http://prom/api/v1/targets").mock(side_effect=httpx.ConnectError("refused"))
    with httpx.Client() as client:
        checks = pipeline.verify_pipeline(
            ENDPOINTS, "some-other-token-0123456789", client, sleep=lambda _: None
        )
    assert checks[0].ok is False
    assert "ConnectError" in checks[0].detail
    assert checks[3].ok is True
    assert checks[4].ok is False
    assert "got 401" in checks[4].detail


def test_silence_payload_matches_one_service_for_the_given_time():
    now = datetime(2026, 11, 12, 8, 0, tzinfo=UTC)
    payload = pipeline.silence_payload("orders", 15, "maintenance", "me", now)
    assert payload["matchers"] == [
        {"name": "service", "value": "orders", "isRegex": False, "isEqual": True}
    ]
    assert payload["startsAt"] == "2026-11-12T08:00:00+00:00"
    assert payload["endsAt"] == "2026-11-12T08:15:00+00:00"
    assert payload["comment"] == "maintenance"


@respx.mock
def test_create_list_and_expire_silences(respx_mock: respx.MockRouter):
    respx_mock.post("http://am/api/v2/silences").respond(json={"silenceID": "abc-123"})
    respx_mock.get("http://am/api/v2/silences").respond(
        json=[
            {"id": "old", "status": {"state": "expired"}, "endsAt": "2026-01-01T00:00:00Z"},
            {"id": "b", "status": {"state": "active"}, "endsAt": "2026-11-12T09:00:00Z"},
            {"id": "a", "status": {"state": "pending"}, "endsAt": "2026-11-12T08:30:00Z"},
        ]
    )
    expired = respx_mock.delete("http://am/api/v2/silence/abc-123").respond(200)
    with httpx.Client() as client:
        assert pipeline.create_silence(client, "http://am", "orders", 15, "c") == "abc-123"
        assert [s["id"] for s in pipeline.list_silences(client, "http://am")] == ["a", "b"]
        pipeline.expire_silence(client, "http://am", "abc-123")
    assert expired.called


@respx.mock
def test_silence_errors_propagate(respx_mock: respx.MockRouter):
    respx_mock.post("http://am/api/v2/silences").respond(400, json={})
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        pipeline.create_silence(client, "http://am", "orders", 15, "c")
