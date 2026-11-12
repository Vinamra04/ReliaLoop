import json
from pathlib import Path

import httpx
import respx

from libs.core.gate import EXIT_BLOCKED, decide
from tools.deploy.run import EventRecorder, current_version, deploy, write_version

AGENT = "http://agent:8080"
TOKEN = "test-webhook-token-0123456789"
ALLOW = decide({"orders-availability": 0.9}, min_budget=0.25)
BLOCK = decide({"orders-availability": 0.1}, min_budget=0.25)
OVERRIDE = decide({"orders-availability": 0.1}, min_budget=0.25, override_reason="demo")


def versions(tmp_path: Path) -> Path:
    path = tmp_path / "versions.env"
    path.write_text("# header\nGATEWAY_VERSION=v1\nORDERS_VERSION=v1\n", encoding="utf-8")
    return path


def run(tmp_path: Path, router: respx.MockRouter, gate=ALLOW, *, healthy=True, image=True):
    events = router.post(f"{AGENT}/events/deploy").respond(202, json={})
    composed: list[str] = []

    def compose_up(service: str) -> bool:
        composed.append(service)
        return healthy

    path = versions(tmp_path)
    with httpx.Client() as client:
        recorder = EventRecorder(client, AGENT, TOKEN, tmp_path / "deploy-events.log")
        result = deploy(
            service="orders",
            version="v2-bad",
            gate=gate,
            versions_file=path,
            recorder=recorder,
            compose_up=compose_up,
            image_exists=lambda _: image,
        )
    sent = [json.loads(call.request.content) for call in events.calls]
    return result, sent, composed, path, recorder


def test_version_file_helpers(tmp_path: Path):
    path = versions(tmp_path)
    assert current_version(path, "orders") == "v1"
    assert current_version(path, "loadgen") is None
    assert current_version(tmp_path / "missing.env", "orders") is None
    write_version(path, "orders", "v2-bad")
    assert (
        path.read_text(encoding="utf-8") == "# header\nGATEWAY_VERSION=v1\nORDERS_VERSION=v2-bad\n"
    )


@respx.mock
def test_successful_deploy_records_started_and_completed(tmp_path, respx_mock):
    result, sent, composed, path, recorder = run(tmp_path, respx_mock)
    assert (result.ok, result.exit_code, result.previous_version) == (True, 0, "v1")
    assert composed == ["orders"]
    assert current_version(path, "orders") == "v2-bad"
    assert [(e["status"], e["version"], e["previous_version"]) for e in sent] == [
        ("started", "v2-bad", "v1"),
        ("completed", "v2-bad", "v1"),
    ]
    assert "override_reason" not in sent[0]
    assert respx_mock.calls[0].request.headers["authorization"] == f"Bearer {TOKEN}"
    assert recorder.used_fallback is False


@respx.mock
def test_blocked_gate_changes_nothing(tmp_path, respx_mock):
    result, sent, composed, path, _ = run(tmp_path, respx_mock, BLOCK)
    assert (result.ok, result.exit_code) == (False, EXIT_BLOCKED)
    assert sent == []
    assert composed == []
    assert current_version(path, "orders") == "v1"


@respx.mock
def test_override_is_recorded_with_the_deploy(tmp_path, respx_mock):
    result, sent, _, _, _ = run(tmp_path, respx_mock, OVERRIDE)
    assert result.ok is True
    assert all("demo" in event["override_reason"] for event in sent)


@respx.mock
def test_unhealthy_deploy_is_recorded_as_failed(tmp_path, respx_mock):
    result, sent, _, path, _ = run(tmp_path, respx_mock, healthy=False)
    assert (result.ok, result.exit_code) == (False, 1)
    assert [e["status"] for e in sent] == ["started", "failed"]
    # The version file still names what is actually running, so a rollback knows its target.
    assert current_version(path, "orders") == "v2-bad"


@respx.mock
def test_missing_image_stops_before_anything_changes(tmp_path, respx_mock):
    result, sent, composed, path, _ = run(tmp_path, respx_mock, image=False)
    assert result.ok is False
    assert "does not exist" in result.message
    assert sent == []
    assert composed == []
    assert current_version(path, "orders") == "v1"


@respx.mock
def test_events_fall_back_to_a_local_file_when_the_agent_is_down(tmp_path, respx_mock):
    respx_mock.post(f"{AGENT}/events/deploy").mock(side_effect=httpx.ConnectError("refused"))
    path = versions(tmp_path)
    log = tmp_path / "data" / "deploy-events.log"
    with httpx.Client() as client:
        recorder = EventRecorder(client, AGENT, TOKEN, log)
        result = deploy(
            service="orders",
            version="v2-bad",
            gate=ALLOW,
            versions_file=path,
            recorder=recorder,
            compose_up=lambda _: True,
            image_exists=lambda _: True,
        )
    assert result.ok is True
    assert recorder.used_fallback is True
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [(e["status"], e["version"]) for e in lines] == [
        ("started", "v2-bad"),
        ("completed", "v2-bad"),
    ]
    assert all("ts" in e for e in lines)
    assert TOKEN not in log.read_text(encoding="utf-8")
