from pathlib import Path
from typing import Any

import pytest

from agent.adapters.docker_exec import (
    ContainerSpec,
    DockerExecutor,
    clone_spec,
    is_healthy,
    to_status,
    update_versions_file,
)
from agent.adapters.kubernetes_exec import KubernetesExecutor
from agent.tests.fakes import cmdb

PROJECT = "relialoop"
IMAGE_ENV = {
    "relialoop/orders:v1": ["PATH=/opt/venv/bin", "RELEASE_VERSION=v1", "RELEASE_PROFILE=good"],
    "relialoop/orders:v2-bad": [
        "PATH=/opt/venv/bin",
        "RELEASE_VERSION=v2-bad",
        "RELEASE_PROFILE=bad",
    ],
    "relialoop/gateway:v1": ["PATH=/opt/venv/bin", "RELEASE_VERSION=v1"],
}
IMAGE_LABELS = {"relialoop/orders:v2-bad": {"relialoop.demo": "fault-release"}}


def inspect(
    service: str = "orders",
    number: int = 1,
    image: str = "relialoop/orders:v1",
    *,
    health: str = "healthy",
    state: str = "running",
    replica: bool = False,
) -> dict[str, Any]:
    labels = {
        "com.docker.compose.project": PROJECT,
        "com.docker.compose.service": service,
        "com.docker.compose.container-number": str(number),
        "relialoop.managed": "true",
        **IMAGE_LABELS.get(image, {}),
    }
    if replica:
        labels["relialoop.replica"] = "true"
    return {
        "Id": f"id-{service}-{number}",
        "Name": f"/{PROJECT}-{service}-{number}",
        "RestartCount": 0,
        "State": {
            "Status": state,
            "Health": {"Status": health},
            "StartedAt": "2026-11-12T07:00:00.123456789Z",
            "OOMKilled": False,
        },
        "Config": {
            "Image": image,
            "Env": [*IMAGE_ENV.get(image, []), "CHAOS_TOKEN=secret-token", "ORDERS_WORKERS=4"],
            "Labels": labels,
            "Healthcheck": {"Test": ["CMD", "python", "-m", "services.kit.healthcheck"]},
        },
        "HostConfig": {
            "Memory": 201326592,
            "NanoCpus": 500000000,
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Tmpfs": {"/tmp": ""},
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "Privileged": False,
            "Binds": None,
        },
        "NetworkSettings": {
            "Networks": {
                "relialoop_internal": {"Aliases": [f"{PROJECT}-{service}-{number}", service]}
            }
        },
    }


class FakeRuntime:
    """In-memory container runtime that behaves like Docker for our purposes."""

    def __init__(self, *containers: dict[str, Any]) -> None:
        self.containers = {c["Id"]: c for c in containers}
        self.images = set(IMAGE_ENV)
        self.calls: list[tuple[str, str]] = []
        self.created: list[ContainerSpec] = []
        self.new_health = "healthy"
        self.fail_on: str | None = None

    def _maybe_fail(self, operation: str) -> None:
        if self.fail_on == operation:
            raise RuntimeError(f"{operation} exploded")

    def list(self, project: str, service: str) -> list[dict[str, Any]]:
        self._maybe_fail("list")
        return [
            c
            for c in self.containers.values()
            if c["Config"]["Labels"].get("com.docker.compose.project") == project
            and c["Config"]["Labels"].get("com.docker.compose.service") == service
        ]

    def restart(self, container_id: str, timeout_s: int) -> None:
        self._maybe_fail("restart")
        self.calls.append(("restart", container_id))

    def remove(self, container_id: str, timeout_s: int) -> None:
        self._maybe_fail("remove")
        self.calls.append(("remove", container_id))
        del self.containers[container_id]

    def create_and_start(self, spec: ContainerSpec) -> str:
        self._maybe_fail("create")
        self.created.append(spec)
        container_id = f"id-{spec.name}"
        self.containers[container_id] = {
            "Id": container_id,
            "Name": f"/{spec.name}",
            "RestartCount": 0,
            "State": {"Status": "running", "Health": {"Status": self.new_health}},
            "Config": {
                "Image": spec.image,
                "Env": [*IMAGE_ENV.get(spec.image, []), *spec.env],
                "Labels": {**IMAGE_LABELS.get(spec.image, {}), **spec.labels},
            },
            "HostConfig": dict(spec.host_config),
            "NetworkSettings": {
                "Networks": {n: {"Aliases": list(a)} for n, a in spec.networks.items()}
            },
        }
        return container_id

    def image_config(self, image: str) -> dict[str, Any] | None:
        if image not in self.images:
            return None
        return {"Env": IMAGE_ENV.get(image, []), "Labels": IMAGE_LABELS.get(image, {})}

    def logs(self, container_id: str, tail: int, since_s: int) -> str:
        return f"line one from {container_id}\n\nline two from {container_id}\n"


async def no_sleep(_: float) -> None:
    return None


def executor(runtime: FakeRuntime, tmp_path: Path, timeout: float = 5.0) -> DockerExecutor:
    return DockerExecutor(
        runtime,
        cmdb(),
        project=PROJECT,
        versions_file=tmp_path / "versions.env",
        action_timeout_s=timeout,
        sleep=no_sleep,
    )


# --- helpers -------------------------------------------------------------------


def test_to_status_and_health():
    status = to_status(inspect())
    assert (status.name, status.image, status.state, status.health) == (
        "relialoop-orders-1",
        "relialoop/orders:v1",
        "running",
        "healthy",
    )
    assert is_healthy(status)
    assert not is_healthy(to_status(inspect(health="starting")))
    assert not is_healthy(to_status(inspect(state="restarting")))
    no_check = inspect()
    del no_check["State"]["Health"]
    assert is_healthy(to_status(no_check))


def test_clone_spec_keeps_config_but_drops_what_belongs_to_the_image():
    attrs = inspect(image="relialoop/orders:v2-bad")
    image_config = {
        "Env": IMAGE_ENV["relialoop/orders:v2-bad"],
        "Labels": IMAGE_LABELS["relialoop/orders:v2-bad"],
    }
    spec = clone_spec(
        attrs,
        image_config,
        service="orders",
        name="relialoop-orders-1",
        image="relialoop/orders:v1",
        number=1,
        replica=False,
    )
    # The bad image's baked-in profile must not follow the container to the good image.
    assert spec.env == ("CHAOS_TOKEN=secret-token", "ORDERS_WORKERS=4")
    assert "relialoop.demo" not in spec.labels
    assert spec.labels["relialoop.managed"] == "true"
    assert spec.labels["com.docker.compose.service"] == "orders"
    assert "relialoop.replica" not in spec.labels
    assert spec.networks == {"relialoop_internal": ("orders",)}
    assert spec.host_config == {
        "Memory": 201326592,
        "NanoCpus": 500000000,
        "ReadonlyRootfs": True,
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
        "Tmpfs": {"/tmp": ""},
        "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
    }
    assert spec.healthcheck == {"Test": ["CMD", "python", "-m", "services.kit.healthcheck"]}


def test_update_versions_file_replaces_or_appends(tmp_path: Path):
    path = tmp_path / "versions.env"
    path.write_text("# comment\nGATEWAY_VERSION=v1\nORDERS_VERSION=v2-bad\n", encoding="utf-8")
    update_versions_file(path, "orders", "v1")
    assert path.read_text(encoding="utf-8") == "# comment\nGATEWAY_VERSION=v1\nORDERS_VERSION=v1\n"
    update_versions_file(path, "loadgen", "v3")
    assert path.read_text(encoding="utf-8").endswith("LOADGEN_VERSION=v3\n")
    fresh = tmp_path / "new.env"
    update_versions_file(fresh, "orders", "v1")
    assert fresh.read_text(encoding="utf-8") == "ORDERS_VERSION=v1\n"


# --- status / logs ---------------------------------------------------------------


async def test_status_lists_only_this_service(tmp_path: Path):
    runtime = FakeRuntime(inspect(), inspect("gateway", image="relialoop/gateway:v1"))
    statuses = await executor(runtime, tmp_path).status("orders")
    assert [s.name for s in statuses] == ["relialoop-orders-1"]


async def test_unmanaged_services_are_refused(tmp_path: Path):
    result = await executor(FakeRuntime(inspect()), tmp_path).restart("postgres")
    assert result.success is False
    assert "not a managed service" in result.detail


async def test_recent_logs_merge_all_replicas(tmp_path: Path):
    runtime = FakeRuntime(inspect(), inspect(number=2, replica=True))
    lines = await executor(runtime, tmp_path).recent("orders", 3, 300)
    assert lines == [
        "line two from id-orders-1",
        "line one from id-orders-2",
        "line two from id-orders-2",
    ]


# --- restart ---------------------------------------------------------------------


async def test_restart_restarts_every_replica_and_waits_for_health(tmp_path: Path):
    runtime = FakeRuntime(inspect(), inspect(number=2, replica=True))
    result = await executor(runtime, tmp_path).restart("orders")
    assert result.success is True
    assert runtime.calls == [("restart", "id-orders-1"), ("restart", "id-orders-2")]
    assert result.data == {"containers": ["relialoop-orders-1", "relialoop-orders-2"]}


async def test_restart_fails_when_the_service_never_becomes_healthy(tmp_path: Path):
    runtime = FakeRuntime(inspect(health="unhealthy"))
    result = await executor(runtime, tmp_path, timeout=0.01).restart("orders")
    assert result.success is False
    assert "timed out" in result.detail
    assert "relialoop-orders-1=running/unhealthy" in result.detail


async def test_restart_without_containers_and_runtime_errors_fail_cleanly(tmp_path: Path):
    assert (await executor(FakeRuntime(), tmp_path).restart("orders")).detail == (
        "no containers found for orders"
    )
    runtime = FakeRuntime(inspect())
    runtime.fail_on = "restart"
    result = await executor(runtime, tmp_path).restart("orders")
    assert result.success is False
    assert "RuntimeError: restart exploded" in result.detail


# --- scale -----------------------------------------------------------------------


async def test_scale_up_clones_the_base_container_with_a_shared_alias(tmp_path: Path):
    runtime = FakeRuntime(inspect())
    result = await executor(runtime, tmp_path).scale("orders", 3)
    assert result.success is True
    assert result.data == {
        "from": 1,
        "to": 3,
        "created": ["relialoop-orders-2", "relialoop-orders-3"],
    }
    for spec, number in zip(runtime.created, ("2", "3"), strict=True):
        assert spec.image == "relialoop/orders:v1"
        assert spec.networks == {"relialoop_internal": ("orders",)}
        assert spec.labels["relialoop.replica"] == "true"
        assert spec.labels["com.docker.compose.container-number"] == number
        assert spec.labels["com.docker.compose.project"] == PROJECT
        assert spec.host_config["Memory"] == 201326592
        assert spec.host_config["CapDrop"] == ["ALL"]
        assert spec.env == ("CHAOS_TOKEN=secret-token", "ORDERS_WORKERS=4")


async def test_scale_up_reuses_free_numbers(tmp_path: Path):
    runtime = FakeRuntime(inspect(number=1), inspect(number=3, replica=True))
    result = await executor(runtime, tmp_path).scale("orders", 3)
    assert result.data["created"] == ["relialoop-orders-2"]


async def test_scale_respects_cmdb_limits(tmp_path: Path):
    runtime = FakeRuntime(inspect(), inspect("gateway", image="relialoop/gateway:v1"))
    exec_ = executor(runtime, tmp_path)
    assert (await exec_.scale("orders", 4)).success is False
    assert (await exec_.scale("orders", 0)).success is False
    assert (await exec_.scale("gateway", 2)).success is False
    assert runtime.created == []


async def test_scale_to_current_size_is_a_no_op(tmp_path: Path):
    runtime = FakeRuntime(inspect())
    result = await executor(runtime, tmp_path).scale("orders", 1)
    assert (result.success, result.detail) == (True, "already at 1")
    assert runtime.created == []


async def test_scale_down_removes_clones_first_and_keeps_the_base(tmp_path: Path):
    runtime = FakeRuntime(
        inspect(number=1), inspect(number=2, replica=True), inspect(number=3, replica=True)
    )
    result = await executor(runtime, tmp_path).scale("orders", 1)
    assert result.success is True
    assert result.data["removed"] == ["relialoop-orders-3", "relialoop-orders-2"]
    assert list(runtime.containers) == ["id-orders-1"]


async def test_scale_up_fails_if_the_new_replica_is_unhealthy(tmp_path: Path):
    runtime = FakeRuntime(inspect())
    runtime.new_health = "unhealthy"
    result = await executor(runtime, tmp_path, timeout=0.01).scale("orders", 2)
    assert result.success is False
    assert result.data["created"] == ["relialoop-orders-2"]


# --- rollback --------------------------------------------------------------------


async def test_rollback_recreates_on_the_previous_image_and_updates_versions(tmp_path: Path):
    versions = tmp_path / "versions.env"
    versions.write_text("GATEWAY_VERSION=v1\nORDERS_VERSION=v2-bad\n", encoding="utf-8")
    runtime = FakeRuntime(
        inspect(image="relialoop/orders:v2-bad"),
        inspect(number=2, image="relialoop/orders:v2-bad", replica=True),
    )
    result = await executor(runtime, tmp_path).rollback("orders", "v1")

    assert result.success is True
    assert result.data == {
        "from_image": "relialoop/orders:v2-bad",
        "to_image": "relialoop/orders:v1",
        "to_version": "v1",
    }
    assert [s.name for s in runtime.created] == ["relialoop-orders-1", "relialoop-orders-2"]
    for spec in runtime.created:
        assert spec.image == "relialoop/orders:v1"
        assert not any(entry.startswith("RELEASE_PROFILE") for entry in spec.env)
        assert "relialoop.demo" not in spec.labels
    assert "relialoop.replica" not in runtime.created[0].labels
    assert runtime.created[1].labels["relialoop.replica"] == "true"
    assert versions.read_text(encoding="utf-8") == "GATEWAY_VERSION=v1\nORDERS_VERSION=v1\n"
    statuses = await executor(runtime, tmp_path).status("orders")
    assert {s.image for s in statuses} == {"relialoop/orders:v1"}


async def test_rollback_refuses_a_missing_image_and_changes_nothing(tmp_path: Path):
    runtime = FakeRuntime(inspect(image="relialoop/orders:v2-bad"))
    exec_ = executor(runtime, tmp_path)
    assert await exec_.version_available("orders", "v1") is True
    assert await exec_.version_available("orders", "v0") is False
    result = await exec_.rollback("orders", "v0")
    assert result.success is False
    assert "relialoop/orders:v0 is not available" in result.detail
    assert runtime.calls == []
    assert not (tmp_path / "versions.env").exists()


async def test_rollback_is_idempotent(tmp_path: Path):
    runtime = FakeRuntime(inspect())
    result = await executor(runtime, tmp_path).rollback("orders", "v1")
    assert (result.success, result.detail) == (True, "already on relialoop/orders:v1")
    assert runtime.calls == []


async def test_rollback_reports_a_failed_recreate(tmp_path: Path):
    runtime = FakeRuntime(inspect(image="relialoop/orders:v2-bad"))
    runtime.fail_on = "create"
    result = await executor(runtime, tmp_path).rollback("orders", "v1")
    assert result.success is False
    assert "create exploded" in result.detail


async def test_version_available_without_containers(tmp_path: Path):
    assert await executor(FakeRuntime(), tmp_path).version_available("orders", "v1") is False


# --- kubernetes stub -------------------------------------------------------------


async def test_kubernetes_executor_is_an_explicit_stub():
    stub = KubernetesExecutor()
    for call in (
        stub.status("orders"),
        stub.restart("orders"),
        stub.scale("orders", 2),
        stub.rollback("orders", "v1"),
        stub.version_available("orders", "v1"),
    ):
        with pytest.raises(NotImplementedError, match="stub"):
            await call
