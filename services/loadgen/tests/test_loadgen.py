import asyncio
import random
from collections import Counter

import httpx
import pytest
from fastapi.testclient import TestClient

from services.loadgen.engine import LoadEngine, RateController, classify
from services.loadgen.main import build_app
from services.loadgen.settings import LoadgenSettings

TOKEN = "test-loadgen-token-0123456789"
AUTH = {"X-Loadgen-Token": TOKEN}


def settings(**overrides) -> LoadgenSettings:
    return LoadgenSettings(loadgen_token=TOKEN, target_url="http://gateway", **overrides)


def ok_handler(request: httpx.Request) -> httpx.Response:
    if request.method == "POST":
        return httpx.Response(201, json={"id": "11111111-1111-1111-1111-111111111111"})
    return httpx.Response(200, json={"orders": []})


def test_classify():
    assert classify(200) == "ok"
    assert classify(201) == "ok"
    assert classify(404) == "client_error"
    assert classify(502) == "error"


def test_rate_controller_surge_expires_back_to_base():
    now = [0.0]
    rate = RateController(20, 500, clock=lambda: now[0])
    rate.set_rate(180, 30)
    assert rate.current() == 180
    assert rate.status()["override_remaining_s"] == 30
    now[0] = 31
    assert rate.current() == 20
    assert rate.status()["override_remaining_s"] is None


def test_rate_controller_base_change_and_cap():
    rate = RateController(20, 500)
    rate.set_rate(10_000, None)
    assert rate.current() == 500
    assert rate.status()["base_rps"] == 500


async def run_engine(handler, count: int, **overrides):
    outcomes: Counter[str] = Counter()
    methods: Counter[str] = Counter()

    def counting(request: httpx.Request) -> httpx.Response:
        methods[request.method] += 1
        return handler(request)

    cfg = settings(**overrides)
    async with httpx.AsyncClient(
        base_url=cfg.target_url, transport=httpx.MockTransport(counting)
    ) as client:
        engine = LoadEngine(
            cfg,
            [client],
            RateController(20, 500),
            lambda outcome, _: outcomes.update([outcome]),
            rng=random.Random(11),
        )
        for _ in range(count):
            await engine.send_one()
    return outcomes, methods


async def test_request_mix_is_roughly_70_30():
    outcomes, methods = await run_engine(ok_handler, 1000)
    assert outcomes == Counter(ok=1000)
    assert 640 < methods["GET"] < 760
    assert methods["GET"] + methods["POST"] == 1000


async def test_outcomes_are_classified():
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert (await run_engine(timeout, 5))[0] == Counter(timeout=5)
    assert (await run_engine(refused, 5))[0] == Counter(connect_error=5)
    assert (await run_engine(lambda r: httpx.Response(502), 5))[0] == Counter(error=5)


async def test_engine_paces_to_the_configured_rate_and_drops_when_full():
    outcomes: Counter[str] = Counter()
    cfg = settings(max_in_flight=1000)
    async with httpx.AsyncClient(
        base_url=cfg.target_url, transport=httpx.MockTransport(ok_handler)
    ) as client:
        engine = LoadEngine(
            cfg,
            [client],
            RateController(200, 500),
            lambda outcome, _: outcomes.update([outcome]),
            rng=random.Random(5),
        )
        task = asyncio.create_task(engine.run())
        await asyncio.sleep(1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    # Generous bounds: timer resolution varies by platform.
    assert 80 < outcomes["ok"] < 320


def test_control_api_requires_token_and_reports_status():
    app = build_app(settings(), transport=httpx.MockTransport(ok_handler), autostart=False)
    with TestClient(app) as client:
        assert client.get("/status").json()["current_rps"] == 20
        assert client.post("/rate", json={"rps": 180, "duration_s": 60}).status_code == 401
        surged = client.post("/rate", json={"rps": 180, "duration_s": 60}, headers=AUTH)
        assert surged.json()["current_rps"] == 180
        assert surged.json()["base_rps"] == 20
        assert client.post("/rate", json={"rps": -1}, headers=AUTH).status_code == 422
        assert client.get("/chaos").status_code == 404
        assert "loadgen_requests_total" in client.get("/metrics").text


def test_token_is_required():
    with pytest.raises(ValueError, match="LOADGEN_TOKEN"):
        build_app(LoadgenSettings(), autostart=False)
