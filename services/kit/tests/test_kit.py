import json
import random

import pytest
from fastapi.testclient import TestClient

from services.kit.app import create_app
from services.kit.chaos import (
    ChaosState,
    ErrorFaultRequest,
    LatencyFaultRequest,
    LeakFaultRequest,
)
from services.kit.settings import ServiceSettings

TOKEN = "test-chaos-token-0123456789"
AUTH = {"X-Chaos-Token": TOKEN}


def make_app(**overrides):
    settings = ServiceSettings(service_name="demo", chaos_token=TOKEN, **overrides)
    app = create_app(settings)

    @app.get("/api/items/{item_id}")
    async def item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    @app.get("/api/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    return app


def metric_lines(client: TestClient, name: str) -> list[str]:
    body = client.get("/metrics").text
    return [line for line in body.splitlines() if line.startswith(name)]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_health_endpoints():
    with TestClient(make_app()) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").json() == {"status": "ready"}


def test_readyz_reports_failures():
    async def not_ready() -> dict[str, str]:
        return {"db": "connection refused"}

    app = create_app(ServiceSettings(service_name="demo", chaos_token=TOKEN), readiness=not_ready)
    with TestClient(app) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["failures"] == {"db": "connection refused"}


def test_metrics_use_route_template_not_raw_path():
    with TestClient(make_app()) as client:
        client.get("/api/items/abc")
        client.get("/api/items/def")
        lines = metric_lines(client, "http_requests_total{")
    assert any(
        'route="/api/items/{item_id}"' in line and 'status="200"' in line and line.endswith(" 2.0")
        for line in lines
    )
    assert not any("abc" in line or "def" in line for line in lines)


def test_unmatched_routes_share_one_label():
    with TestClient(make_app()) as client:
        client.get("/nope/1")
        client.get("/nope/2")
        lines = metric_lines(client, "http_requests_total{")
    assert any('route="__unmatched__"' in line and 'status="404"' in line for line in lines)


def test_unhandled_exception_is_counted_as_500():
    with TestClient(make_app(), raise_server_exceptions=False) as client:
        assert client.get("/api/boom").status_code == 500
        lines = metric_lines(client, "http_requests_total{")
    assert any('route="/api/boom"' in line and 'status="500"' in line for line in lines)


def test_histogram_has_slo_buckets_and_build_info():
    with TestClient(make_app(release_version="v9")) as client:
        client.get("/api/items/x")
        body = client.get("/metrics").text
    assert 'le="0.3"' in body
    assert 'le="0.5"' in body
    assert 'service_build_info{release_profile="good",version="v9"} 1.0' in body
    assert "http_requests_in_flight" in body


def test_request_id_is_echoed_and_unsafe_ids_replaced():
    with TestClient(make_app()) as client:
        assert client.get("/healthz", headers={"X-Request-ID": "abc-123"}).headers[
            "x-request-id"
        ] == ("abc-123")
        replaced = client.get("/healthz", headers={"X-Request-ID": 'bad id "x"'}).headers[
            "x-request-id"
        ]
    assert replaced != 'bad id "x"'
    assert len(replaced) == 32


def test_request_log_line_is_json_with_route(capsys):
    with TestClient(make_app()) as client:
        client.get("/api/items/42", headers={"X-Request-ID": "rid-1"})
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    request_logs = [r for r in records if r.get("event") == "request"]
    assert request_logs[-1]["route"] == "/api/items/{item_id}"
    assert request_logs[-1]["request_id"] == "rid-1"
    assert request_logs[-1]["service"] == "demo"
    assert request_logs[-1]["status"] == 200


def test_chaos_requires_token():
    with TestClient(make_app()) as client:
        assert client.get("/chaos").status_code == 401
        assert (
            client.post("/chaos/errors", json={}, headers={"X-Chaos-Token": "wrong"}).status_code
            == 401
        )
        assert client.get("/chaos", headers=AUTH).status_code == 200


def test_chaos_errors_hit_business_routes_only_and_clear():
    with TestClient(make_app()) as client:
        body = {"rate": 1.0, "status": 503, "message": "db exploded"}
        assert client.post("/chaos/errors", json=body, headers=AUTH).status_code == 200
        assert client.get("/api/items/1").status_code == 503
        assert client.get("/healthz").status_code == 200
        assert client.get("/metrics").status_code == 200
        assert client.get("/chaos", headers=AUTH).json()["active"]["errors"]["rate"] == 1.0
        lines = metric_lines(client, "http_requests_total{")
        assert any('route="/api/items/{item_id}"' in ln and 'status="503"' in ln for ln in lines)
        assert client.delete("/chaos", headers=AUTH).json() == {"active": {}}
        assert client.get("/api/items/1").status_code == 200


def test_chaos_error_message_is_logged(capsys):
    with TestClient(make_app()) as client:
        client.post("/chaos/errors", json={"rate": 1.0, "message": "custom failure"}, headers=AUTH)
        client.get("/api/items/1")
    assert '"event": "custom failure"' in capsys.readouterr().out


def test_chaos_rejects_invalid_payload():
    with TestClient(make_app()) as client:
        assert client.post("/chaos/errors", json={"rate": 2}, headers=AUTH).status_code == 422


def test_faults_expire():
    clock = FakeClock()
    state = ChaosState(300, 3600, clock=clock, rng=random.Random(1))
    state.set_errors(ErrorFaultRequest(rate=1.0, duration_s=10))
    state.set_latency(LatencyFaultRequest(ms=100, jitter_ms=0))
    assert state.error_to_raise() == (500, "internal error: request processing failed")
    assert state.latency_seconds() == pytest.approx(0.1)
    clock.now += 11
    assert state.error_to_raise() is None
    assert "latency" in state.active()
    clock.now += 300
    assert state.active() == {}


def test_duration_is_capped():
    clock = FakeClock()
    state = ChaosState(300, 600, clock=clock)
    state.set_errors(ErrorFaultRequest(duration_s=100_000))
    assert state.active()["errors"]["remaining_s"] == 600


def test_error_rate_is_respected():
    state = ChaosState(300, 3600, rng=random.Random(7))
    state.set_errors(ErrorFaultRequest(rate=0.5))
    failures = sum(1 for _ in range(2000) if state.error_to_raise())
    assert 900 < failures < 1100


def test_leak_grows_to_cap_and_keeps_memory_after_expiry():
    clock = FakeClock()
    state = ChaosState(300, 3600, clock=clock)
    state.set_leak(LeakFaultRequest(mb_per_s=2, max_mb=5, duration_s=60))
    for _ in range(5):
        state.leak_tick(1.0)
    assert state.leaked_mb == pytest.approx(5.0)
    clock.now += 61
    state.leak_tick(1.0)
    assert state.active() == {"leaked_mb": 5.0}
    state.clear()
    assert state.leaked_mb == 0


def test_bad_release_profile_fails_some_requests():
    app = make_app(release_profile="bad", bad_release_error_rate=1.0, bad_release_latency_ms=0)
    with TestClient(app) as client:
        assert client.get("/api/items/1").status_code == 500
        assert client.get("/healthz").status_code == 200


def test_chaos_token_is_required_when_chaos_enabled():
    with pytest.raises(ValueError, match="CHAOS_TOKEN"):
        create_app(ServiceSettings(service_name="demo"))
    app = create_app(ServiceSettings(service_name="demo"), enable_chaos=False)
    with TestClient(app) as client:
        assert client.get("/chaos").status_code == 404
