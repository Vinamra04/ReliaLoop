"""Docker implementation of the `ActionExecutor` and `LogSource` ports.

Two layers:

- `ContainerRuntime` is the small set of container operations the executor
  needs. `DockerSdkRuntime` implements it with the Docker SDK; tests use an
  in-memory fake.
- `DockerExecutor` holds the logic: which containers belong to a service,
  how to clone one into a replica, how to roll back, when to give up.

The executor only ever touches containers that carry this Compose project's
labels, so nothing else on the host can be affected.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

from agent.app.domain import ActionResult, ContainerStatus
from agent.ports.protocols import SourceError
from libs.core.cmdb import Cmdb

T = TypeVar("T")

LABEL_PROJECT = "com.docker.compose.project"
LABEL_SERVICE = "com.docker.compose.service"
LABEL_NUMBER = "com.docker.compose.container-number"
LABEL_REPLICA = "relialoop.replica"
STOP_TIMEOUT_S = 10
HEALTH_POLL_S = 1.0
# HostConfig fields copied to a cloned container: limits and hardening.
HOST_CONFIG_FIELDS = (
    "Memory",
    "NanoCpus",
    "ReadonlyRootfs",
    "CapDrop",
    "SecurityOpt",
    "Tmpfs",
    "RestartPolicy",
    "LogConfig",
    "GroupAdd",
)


@dataclass(frozen=True)
class ContainerSpec:
    """Everything needed to create a container equivalent to an existing one."""

    name: str
    image: str
    env: tuple[str, ...]
    labels: dict[str, str]
    networks: dict[str, tuple[str, ...]]
    host_config: dict[str, Any] = field(default_factory=dict)
    healthcheck: dict[str, Any] | None = None


class ContainerRuntime(Protocol):
    """Synchronous container operations (run in a thread by the executor)."""

    def list(self, project: str, service: str) -> list[dict[str, Any]]:
        """Inspect data of every container of a service, running or not."""
        ...

    def restart(self, container_id: str, timeout_s: int) -> None: ...
    def remove(self, container_id: str, timeout_s: int) -> None: ...
    def create_and_start(self, spec: ContainerSpec) -> str: ...
    def image_config(self, image: str) -> dict[str, Any] | None:
        """`Config` of a local image, or None when the image is not present."""
        ...

    def logs(self, container_id: str, tail: int, since_s: int) -> str: ...


def _number(attrs: dict[str, Any]) -> int:
    try:
        return int(attrs["Config"]["Labels"].get(LABEL_NUMBER, "0"))
    except (TypeError, ValueError):
        return 0


def _name(attrs: dict[str, Any]) -> str:
    return str(attrs["Name"]).lstrip("/")


def to_status(attrs: dict[str, Any]) -> ContainerStatus:
    """Reduce inspect data to the facts the agent uses."""
    state = attrs.get("State", {})
    return ContainerStatus(
        name=_name(attrs),
        image=attrs["Config"]["Image"],
        state=state.get("Status", "unknown"),
        health=(state.get("Health") or {}).get("Status", "none"),
        restart_count=int(attrs.get("RestartCount", 0)),
        started_at=state.get("StartedAt", ""),
        oom_killed=bool(state.get("OOMKilled", False)),
    )


def is_healthy(status: ContainerStatus) -> bool:
    """Running, and healthy if the container defines a healthcheck."""
    return status.state == "running" and status.health in ("healthy", "none")


def clone_spec(
    attrs: dict[str, Any],
    image_config: dict[str, Any],
    *,
    service: str,
    name: str,
    image: str,
    number: int,
    replica: bool,
) -> ContainerSpec:
    """Build the spec of a container equivalent to `attrs`, with a new name or image.

    Environment variables and labels that come from the *image* are left out:
    they belong to the image (for example its baked-in release profile), and
    copying them would carry an old image's settings into a new one.
    """
    config = attrs["Config"]
    image_env = set(image_config.get("Env") or [])
    image_labels = image_config.get("Labels") or {}
    env = tuple(entry for entry in config.get("Env") or [] if entry not in image_env)
    labels = {
        key: value
        for key, value in (config.get("Labels") or {}).items()
        if image_labels.get(key) != value
    }
    labels[LABEL_NUMBER] = str(number)
    if replica:
        labels[LABEL_REPLICA] = "true"
    host = attrs.get("HostConfig", {})
    return ContainerSpec(
        name=name,
        image=image,
        env=env,
        labels=labels,
        # Every replica answers to the service name: that shared alias is how
        # the gateway and Prometheus discover replicas.
        networks=dict.fromkeys(attrs["NetworkSettings"]["Networks"], (service,)),
        host_config={key: host[key] for key in HOST_CONFIG_FIELDS if host.get(key)},
        healthcheck=config.get("Healthcheck"),
    )


def update_versions_file(path: Path, service: str, version: str) -> None:
    """Set `<SERVICE>_VERSION` in `deploy/versions.env` so Compose stays consistent."""
    key = f"{service.upper().replace('-', '_')}_VERSION"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replaced = False
    for index, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[index] = f"{key}={version}"
            replaced = True
    if not replaced:
        lines.append(f"{key}={version}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


class DockerExecutor:
    """Restart, scale and roll back managed services through the container runtime."""

    def __init__(
        self,
        runtime: ContainerRuntime,
        cmdb: Cmdb,
        *,
        project: str,
        versions_file: Path,
        action_timeout_s: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._runtime = runtime
        self._cmdb = cmdb
        self._project = project
        self._versions_file = versions_file
        self._timeout_s = action_timeout_s
        self._sleep = sleep

    # --- helpers --------------------------------------------------------------

    def _compose_service(self, service: str) -> str:
        entry = self._cmdb.services.get(service)
        if entry is None or not entry.managed or not entry.compose_service:
            raise SourceError(f"{service} is not a managed service")
        return entry.compose_service

    async def _call(self, fn: Callable[..., T], *args: Any) -> T:
        try:
            return await asyncio.to_thread(fn, *args)
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"container runtime error: {type(exc).__name__}: {exc}") from exc

    async def _containers(self, service: str) -> list[dict[str, Any]]:
        found = await self._call(self._runtime.list, self._project, self._compose_service(service))
        return sorted(found, key=_number)

    async def _wait_healthy(self, service: str, expected: int, deadline: float) -> tuple[bool, str]:
        detail = "no containers"
        while True:
            statuses = [to_status(attrs) for attrs in await self._containers(service)]
            unhealthy = [f"{s.name}={s.state}/{s.health}" for s in statuses if not is_healthy(s)]
            if len(statuses) >= expected and not unhealthy:
                return True, f"{len(statuses)} container(s) healthy"
            detail = f"waiting for {unhealthy or 'containers to appear'}"
            if time.monotonic() >= deadline:
                return False, f"timed out {detail}"
            await self._sleep(HEALTH_POLL_S)

    async def _bounded(
        self, name: str, work: Callable[[float], Awaitable[ActionResult]]
    ) -> ActionResult:
        """Run an action with a deadline and turn every failure into a result."""
        started = time.monotonic()
        deadline = started + self._timeout_s
        try:
            result = await asyncio.wait_for(work(deadline), timeout=self._timeout_s + 5)
        except TimeoutError:
            result = ActionResult(success=False, detail=f"{name} timed out")
        except SourceError as exc:
            result = ActionResult(success=False, detail=str(exc))
        return result.model_copy(update={"duration_s": round(time.monotonic() - started, 2)})

    # --- port: status -----------------------------------------------------------

    async def status(self, service: str) -> list[ContainerStatus]:
        return [to_status(attrs) for attrs in await self._containers(service)]

    # --- port: restart ----------------------------------------------------------

    async def restart(self, service: str) -> ActionResult:
        """Restart every container of the service, then wait until all are healthy."""

        async def work(deadline: float) -> ActionResult:
            containers = await self._containers(service)
            if not containers:
                return ActionResult(success=False, detail=f"no containers found for {service}")
            for attrs in containers:
                await self._call(self._runtime.restart, attrs["Id"], STOP_TIMEOUT_S)
            healthy, detail = await self._wait_healthy(service, len(containers), deadline)
            names = [_name(attrs) for attrs in containers]
            return ActionResult(
                success=healthy, detail=f"restarted {names}: {detail}", data={"containers": names}
            )

        return await self._bounded("restart", work)

    # --- port: scale ------------------------------------------------------------

    async def scale(self, service: str, replicas: int) -> ActionResult:
        """Scale to `replicas` by cloning the base container or removing clones."""

        async def work(deadline: float) -> ActionResult:
            limits = self._cmdb.services[service].limits
            if not limits.min_replicas <= replicas <= limits.max_replicas:
                return ActionResult(
                    success=False,
                    detail=f"{replicas} replicas is outside "
                    f"{limits.min_replicas}-{limits.max_replicas} for {service}",
                )
            containers = await self._containers(service)
            if not containers:
                return ActionResult(success=False, detail=f"no containers found for {service}")
            current = len(containers)
            data: dict[str, Any] = {"from": current, "to": replicas}
            if replicas == current:
                return ActionResult(success=True, detail=f"already at {current}", data=data)

            if replicas > current:
                template = containers[0]
                image = template["Config"]["Image"]
                image_config = await self._call(self._runtime.image_config, image) or {}
                used = {_number(attrs) for attrs in containers}
                created: list[str] = []
                number = 0
                while len(created) < replicas - current:
                    number += 1
                    if number in used:
                        continue
                    spec = clone_spec(
                        template,
                        image_config,
                        service=service,
                        name=f"{self._project}-{self._compose_service(service)}-{number}",
                        image=image,
                        number=number,
                        replica=True,
                    )
                    await self._call(self._runtime.create_and_start, spec)
                    created.append(spec.name)
                healthy, detail = await self._wait_healthy(service, replicas, deadline)
                return ActionResult(
                    success=healthy,
                    detail=f"scaled {service} {current} -> {replicas}, created {created}: {detail}",
                    data={**data, "created": created},
                )

            # Scale down: clones go first (newest first), the base container last.
            removable = sorted(
                containers,
                key=lambda a: (a["Config"]["Labels"].get(LABEL_REPLICA) == "true", _number(a)),
                reverse=True,
            )[: current - replicas]
            removed = []
            for attrs in removable:
                await self._call(self._runtime.remove, attrs["Id"], STOP_TIMEOUT_S)
                removed.append(_name(attrs))
            return ActionResult(
                success=True,
                detail=f"scaled {service} {current} -> {replicas}, removed {removed}",
                data={**data, "removed": removed},
            )

        return await self._bounded("scale", work)

    # --- port: rollback ---------------------------------------------------------

    def _image_for(self, attrs: dict[str, Any], version: str) -> str:
        repository = str(attrs["Config"]["Image"]).rsplit(":", 1)[0]
        return f"{repository}:{version}"

    async def version_available(self, service: str, version: str) -> bool:
        containers = await self._containers(service)
        if not containers:
            return False
        image = self._image_for(containers[0], version)
        return await self._call(self._runtime.image_config, image) is not None

    async def rollback(self, service: str, to_version: str) -> ActionResult:
        """Recreate every container of the service on the image of `to_version`."""

        async def work(deadline: float) -> ActionResult:
            containers = await self._containers(service)
            if not containers:
                return ActionResult(success=False, detail=f"no containers found for {service}")
            old_image = containers[0]["Config"]["Image"]
            new_image = self._image_for(containers[0], to_version)
            data = {"from_image": old_image, "to_image": new_image, "to_version": to_version}
            if await self._call(self._runtime.image_config, new_image) is None:
                return ActionResult(
                    success=False, detail=f"image {new_image} is not available locally", data=data
                )
            if all(attrs["Config"]["Image"] == new_image for attrs in containers):
                return ActionResult(success=True, detail=f"already on {new_image}", data=data)

            old_config = await self._call(self._runtime.image_config, old_image) or {}
            for attrs in containers:
                spec = clone_spec(
                    attrs,
                    old_config,
                    service=service,
                    name=_name(attrs),
                    image=new_image,
                    number=_number(attrs),
                    replica=attrs["Config"]["Labels"].get(LABEL_REPLICA) == "true",
                )
                await self._call(self._runtime.remove, attrs["Id"], STOP_TIMEOUT_S)
                await self._call(self._runtime.create_and_start, spec)
            await asyncio.to_thread(update_versions_file, self._versions_file, service, to_version)
            healthy, detail = await self._wait_healthy(service, len(containers), deadline)
            return ActionResult(
                success=healthy,
                detail=f"rolled {service} back {old_image} -> {new_image}: {detail}",
                data=data,
            )

        return await self._bounded("rollback", work)

    # --- port: logs -------------------------------------------------------------

    async def recent(self, service: str, lines: int, since_s: int) -> list[str]:
        """Recent log lines of every container of the service (untrusted text)."""
        collected: list[str] = []
        for attrs in await self._containers(service):
            text = await self._call(self._runtime.logs, attrs["Id"], lines, since_s)
            collected.extend(line for line in text.splitlines() if line.strip())
        return collected[-lines:]


class DockerSdkRuntime:
    """`ContainerRuntime` backed by the Docker SDK and the mounted Docker socket."""

    def __init__(self, base_url: str = "unix:///var/run/docker.sock", timeout_s: int = 30) -> None:
        import docker

        self._docker = docker
        self._client = docker.DockerClient(base_url=base_url, timeout=timeout_s)

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:
            return False

    def list(self, project: str, service: str) -> list[dict[str, Any]]:
        found = self._client.containers.list(
            all=True,
            filters={"label": [f"{LABEL_PROJECT}={project}", f"{LABEL_SERVICE}={service}"]},
        )
        return [container.attrs for container in found]

    def restart(self, container_id: str, timeout_s: int) -> None:
        self._client.api.restart(container_id, timeout=timeout_s)

    def remove(self, container_id: str, timeout_s: int) -> None:
        self._client.api.stop(container_id, timeout=timeout_s)
        self._client.api.remove_container(container_id, force=True)

    def create_and_start(self, spec: ContainerSpec) -> str:
        api = self._client.api
        host = spec.host_config
        host_config = api.create_host_config(
            mem_limit=host.get("Memory"),
            nano_cpus=host.get("NanoCpus"),
            read_only=host.get("ReadonlyRootfs"),
            cap_drop=host.get("CapDrop"),
            security_opt=host.get("SecurityOpt"),
            tmpfs=host.get("Tmpfs"),
            restart_policy=host.get("RestartPolicy"),
            log_config=host.get("LogConfig"),
            group_add=host.get("GroupAdd"),
        )
        networks = list(spec.networks.items())
        first_network, first_aliases = networks[0]
        created = api.create_container(
            image=spec.image,
            name=spec.name,
            environment=list(spec.env),
            labels=spec.labels,
            healthcheck=spec.healthcheck,
            host_config=host_config,
            networking_config=api.create_networking_config(
                {first_network: api.create_endpoint_config(aliases=list(first_aliases))}
            ),
        )
        container_id = str(created["Id"])
        for network, aliases in networks[1:]:
            api.connect_container_to_network(container_id, network, aliases=list(aliases))
        api.start(container_id)
        return container_id

    def image_config(self, image: str) -> dict[str, Any] | None:
        try:
            config = self._client.images.get(image).attrs.get("Config")
        except self._docker.errors.ImageNotFound:
            return None
        return dict(config or {})

    def logs(self, container_id: str, tail: int, since_s: int) -> str:
        raw = self._client.api.logs(
            container_id, tail=tail, since=int(time.time()) - since_s, stdout=True, stderr=True
        )
        return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
