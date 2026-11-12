"""ReliaLoop command line: the canonical interface to the whole project.

The Makefile is only a thin wrapper over these commands, so everything works
on machines without `make` (native Windows). Commands are added phase by
phase; `--help` always lists exactly what exists.
"""

from __future__ import annotations

import contextlib
import json
from enum import StrEnum
from typing import Any

import httpx
import typer
import yaml
from rich.console import Console

from tools import containers, init_env, paths, pipeline, stack
from tools.dashboards import generate as dashboard_generator
from tools.slo import build as slo_build
from tools.slo.models import SpecError

app = typer.Typer(
    name="relialoop",
    help="ReliaLoop: SLOs as code, alerting, chaos and an evaluated AIOps agent.",
    no_args_is_help=True,
    add_completion=False,
)
dashboards_app = typer.Typer(help="Grafana dashboards as code.", no_args_is_help=True)
app.add_typer(dashboards_app, name="dashboards")
slo_app = typer.Typer(help="SLOs as code: generate and validate rules.", no_args_is_help=True)
app.add_typer(slo_app, name="slo")
console = Console()

HTTP_TIMEOUT_S = 10.0


class Fault(StrEnum):
    errors = "errors"
    latency = "latency"
    leak = "leak"
    crash = "crash"


class ChaosTarget(StrEnum):
    orders = "orders"
    gateway = "gateway"


@app.callback()
def main() -> None:
    """ReliaLoop: SLOs as code, alerting, chaos and an evaluated AIOps agent."""


def _fail(message: str) -> typer.Exit:
    console.print(f"[red]error:[/red] {message}")
    return typer.Exit(code=1)


def _request(method: str, url: str, **kwargs: Any) -> Any:
    """Call a local stack endpoint and return its JSON, or exit with a clear error."""
    try:
        response = httpx.request(method, url, timeout=HTTP_TIMEOUT_S, **kwargs)
    except httpx.HTTPError as exc:
        raise _fail(f"{method} {url} failed: {type(exc).__name__} (is the stack up?)") from exc
    if response.status_code >= 400:
        raise _fail(f"{method} {url} returned {response.status_code}: {response.text[:300]}")
    return response.json()


def _chaos_url(env: dict[str, str], target: ChaosTarget, suffix: str = "") -> str:
    base = stack.local_url("GATEWAY_PORT", env)
    prefix = "/chaos" if target is ChaosTarget.gateway else "/chaos/target/orders"
    return f"{base}{prefix}{suffix}"


@app.command()
def init() -> None:
    """Create .env with random service tokens, the webhook credentials file and runtime dirs."""
    result = init_env.run_init(
        paths.ENV_EXAMPLE, paths.ENV_FILE, paths.WEBHOOK_TOKEN_FILE, paths.RUNTIME_DIRS
    )
    state = "created" if result.env_created else "kept (existing values preserved)"
    console.print(f".env {state}")
    if result.generated_keys:
        console.print(f"generated tokens for: {', '.join(result.generated_keys)}")
    if result.added_keys:
        console.print(f"added new keys from .env.example: {', '.join(result.added_keys)}")
    webhook_state = "written" if result.webhook_file_written else "unchanged"
    console.print(f".secrets/webhook_token {webhook_state}")
    console.print("runtime directories ready")


@app.command()
def build() -> None:
    """Build every application image, including the simulated bad releases of orders."""
    try:
        stack.compose("--profile", "build", "build")
    except stack.StackError as exc:
        raise _fail(str(exc)) from exc


@app.command()
def up(
    rebuild: bool = typer.Option(False, "--build", help="Rebuild images before starting."),
) -> None:
    """Start the stack and wait until every service is healthy."""
    try:
        missing = stack.missing_images()
        if rebuild or missing:
            if missing:
                console.print(f"building missing images: {', '.join(missing)}")
            stack.compose("--profile", "build", "build")
        stack.compose("up", "-d", "--wait", "--remove-orphans")
        env = stack.load_env()
    except stack.StackError as exc:
        raise _fail(str(exc)) from exc
    # Config files are bind-mounted, so containers that were already running
    # must be told to re-read them.
    for key in ("PROMETHEUS_PORT", "ALERTMANAGER_PORT"):
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{stack.local_url(key, env)}/-/reload", timeout=HTTP_TIMEOUT_S)
    console.print("[green]stack is healthy[/green]")
    for name, key in (
        ("gateway", "GATEWAY_PORT"),
        ("agent", "AGENT_PORT"),
        ("prometheus", "PROMETHEUS_PORT"),
        ("alertmanager", "ALERTMANAGER_PORT"),
        ("grafana", "GRAFANA_PORT"),
    ):
        console.print(f"  {name:<11} {stack.local_url(key, env)}")


@app.command()
def down(
    volumes: bool = typer.Option(False, "--volumes", "-v", help="Also delete this project's data."),
) -> None:
    """Stop the stack (this project's containers only)."""
    args = ["down", "--remove-orphans"]
    if volumes:
        args.append("--volumes")
    try:
        stack.compose(*args)
    except stack.StackError as exc:
        raise _fail(str(exc)) from exc


@app.command()
def ps() -> None:
    """Show the stack's containers."""
    stack.compose("ps", "--all", check=False)


@app.command()
def logs(
    service: str = typer.Argument("", help="Service name; empty for all."),
    tail: int = typer.Option(50, help="Lines per container."),
    follow: bool = typer.Option(False, "--follow", "-f"),
) -> None:
    """Show container logs."""
    args = ["logs", "--tail", str(tail)]
    if follow:
        args.append("--follow")
    if service:
        args.append(service)
    stack.compose(*args, check=False)


@app.command()
def chaos(
    fault: Fault = typer.Argument(..., help="errors | latency | leak | crash"),
    service: ChaosTarget = typer.Option(ChaosTarget.orders, help="Service to break."),
    duration: int | None = typer.Option(None, help="Seconds until the fault auto-expires."),
    rate: float = typer.Option(0.5, help="errors: share of requests that fail."),
    status: int = typer.Option(500, help="errors: status code returned."),
    message: str | None = typer.Option(None, help="errors: log message of injected errors."),
    ms: int = typer.Option(800, help="latency: delay in milliseconds."),
    jitter_ms: int = typer.Option(200, help="latency: +/- jitter in milliseconds."),
    mb_per_s: float = typer.Option(2.0, help="leak: growth rate."),
    max_mb: int = typer.Option(600, help="leak: stop growing at this size."),
    after_s: float = typer.Option(5.0, help="crash: delay before the process exits."),
) -> None:
    """Inject a fault into a service (orders: every replica)."""
    body: dict[str, Any]
    if fault is Fault.errors:
        body = {"rate": rate, "status": status}
        if message is not None:
            body["message"] = message
    elif fault is Fault.latency:
        body = {"ms": ms, "jitter_ms": jitter_ms}
    elif fault is Fault.leak:
        body = {"mb_per_s": mb_per_s, "max_mb": max_mb}
    else:
        body = {"after_s": after_s}
    if duration is not None and fault is not Fault.crash:
        body["duration_s"] = duration
    try:
        env = stack.load_env()
    except stack.StackError as exc:
        raise _fail(str(exc)) from exc
    result = _request(
        "POST",
        _chaos_url(env, service, f"/{fault.value}"),
        json=body,
        headers={"X-Chaos-Token": env["CHAOS_TOKEN"]},
    )
    console.print(f"[yellow]fault injected:[/yellow] {fault.value} on {service.value}")
    console.print_json(json.dumps(result))


@app.command("chaos-clear")
def chaos_clear() -> None:
    """Clear every active fault on gateway and on all orders replicas."""
    try:
        env = stack.load_env()
    except stack.StackError as exc:
        raise _fail(str(exc)) from exc
    headers = {"X-Chaos-Token": env["CHAOS_TOKEN"]}
    for target in ChaosTarget:
        _request("DELETE", _chaos_url(env, target), headers=headers)
        console.print(f"cleared faults on {target.value}")


@app.command()
def traffic(
    rps: float = typer.Option(..., help="Requests per second."),
    duration: float | None = typer.Option(
        None, help="Surge length in seconds; omit to change the base rate."
    ),
) -> None:
    """Set the load generator's rate (a temporary surge when --duration is given)."""
    try:
        env = stack.load_env()
    except stack.StackError as exc:
        raise _fail(str(exc)) from exc
    body: dict[str, Any] = {"rps": rps}
    if duration is not None:
        body["duration_s"] = duration
    result = _request(
        "POST",
        f"{stack.local_url('LOADGEN_PORT', env)}/rate",
        json=body,
        headers={"X-Loadgen-Token": env["LOADGEN_TOKEN"]},
    )
    console.print_json(json.dumps(result))


def _relative(path: Any) -> str:
    return str(path.relative_to(paths.REPO_ROOT).as_posix())


def _report(name: str, result: containers.ToolResult) -> bool:
    """Print one check's outcome; return whether it passed."""
    if result.ok:
        console.print(f"[green]ok[/green]    {name}")
    else:
        console.print(f"[red]FAIL[/red]  {name}")
        console.print(result.output, markup=False, highlight=False)
    return result.ok


@slo_app.command("generate")
def slo_generate(
    check: bool = typer.Option(False, "--check", help="Fail if generated files are stale."),
) -> None:
    """Generate Prometheus rules, the SLO JSON schema and docs/slo-design.md from slo/*.yaml."""
    try:
        if check:
            stale = slo_build.stale()
            if stale:
                names = ", ".join(_relative(path) for path in stale)
                raise _fail(f"stale generated files: {names} (run `slo generate`)")
            console.print("generated SLO files are up to date")
            return
        changed = slo_build.write_all()
    except SpecError as exc:
        raise _fail(str(exc)) from exc
    if changed:
        for path in changed:
            console.print(f"updated {_relative(path)}")
    else:
        console.print("generated SLO files already up to date")


@slo_app.command("validate")
def slo_validate() -> None:
    """Validate the specs, check generated files are current and run promtool checks."""
    try:
        stale = slo_build.stale()
    except SpecError as exc:
        raise _fail(str(exc)) from exc
    console.print("[green]ok[/green]    SLO specs match the schema")
    if stale:
        raise _fail(f"stale generated files: {', '.join(_relative(path) for path in stale)}")
    console.print("[green]ok[/green]    generated files are up to date")
    rule_files = containers.relative_files(paths.PROMETHEUS_DIR, "rules/*/*.yml")
    passed = _report("promtool check rules", containers.promtool("check", "rules", *rule_files))
    passed &= _report(
        "promtool check config", containers.promtool("check", "config", "prometheus.yml")
    )
    if not passed:
        raise typer.Exit(code=1)


@app.command("test-rules")
def test_rules() -> None:
    """Run promtool rule unit tests and amtool config and routing tests."""
    rule_tests = containers.relative_files(paths.PROMETHEUS_DIR, "tests/*.yml")
    passed = _report(
        f"promtool test rules ({len(rule_tests)} files)",
        containers.promtool("test", "rules", *rule_tests),
    )
    passed &= _report("amtool check-config", containers.amtool("check-config", "alertmanager.yml"))
    cases = yaml.safe_load(
        (paths.ALERTMANAGER_DIR / "tests" / "routes.yml").read_text(encoding="utf-8")
    )["cases"]
    for case in cases:
        labels = [f"{key}={value}" for key, value in case["labels"].items()]
        result = containers.amtool(
            "config",
            "routes",
            "test",
            "--config.file=alertmanager.yml",
            f"--verify.receivers={case['receiver']}",
            *labels,
        )
        passed &= _report(f"route: {case['name']} -> {case['receiver']}", result)
    if not passed:
        raise typer.Exit(code=1)


def _endpoints(env: dict[str, str]) -> pipeline.Endpoints:
    return pipeline.Endpoints(
        prometheus=stack.local_url("PROMETHEUS_PORT", env),
        alertmanager=stack.local_url("ALERTMANAGER_PORT", env),
        agent=stack.local_url("AGENT_PORT", env),
    )


@app.command("verify-pipeline")
def verify_pipeline() -> None:
    """Check the alerting pipeline end to end: targets, Watchdog, routing and webhook auth."""
    try:
        env = stack.load_env()
    except stack.StackError as exc:
        raise _fail(str(exc)) from exc
    with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
        checks = pipeline.verify_pipeline(_endpoints(env), env["WEBHOOK_TOKEN"], client)
    for check in checks:
        mark = "[green]ok[/green]  " if check.ok else "[red]FAIL[/red]"
        console.print(f"{mark}  {check.name}: ", end="")
        console.print(check.detail, markup=False, highlight=False)
    if not all(check.ok for check in checks):
        raise typer.Exit(code=1)


@app.command()
def silence(
    service: str = typer.Option("", help="Silence every alert of this service."),
    minutes: int = typer.Option(15, min=1, max=1440, help="How long the silence lasts."),
    comment: str = typer.Option("planned maintenance", help="Why (shown in Alertmanager)."),
    list_: bool = typer.Option(False, "--list", help="List active and pending silences."),
    expire: str = typer.Option("", help="Expire the silence with this id."),
) -> None:
    """Create, list or expire Alertmanager silences (for planned maintenance)."""
    try:
        alertmanager = stack.local_url("ALERTMANAGER_PORT", stack.load_env())
    except stack.StackError as exc:
        raise _fail(str(exc)) from exc
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            if list_:
                silences = pipeline.list_silences(client, alertmanager)
                for item in silences:
                    matchers = ", ".join(f"{m['name']}={m['value']}" for m in item["matchers"])
                    console.print(
                        f"{item['id']}  {item['status']['state']:<8} until {item['endsAt']}  "
                        f"{matchers}  ({item['comment']})",
                        markup=False,
                        highlight=False,
                    )
                if not silences:
                    console.print("no active silences")
            elif expire:
                pipeline.expire_silence(client, alertmanager, expire)
                console.print(f"expired silence {expire}")
            elif service:
                silence_id = pipeline.create_silence(
                    client, alertmanager, service, minutes, comment
                )
                console.print(f"silenced service={service} for {minutes} min (id {silence_id})")
            else:
                raise _fail("give --service, --list or --expire")
    except httpx.HTTPError as exc:
        raise _fail(f"Alertmanager request failed: {type(exc).__name__}: {exc}") from exc


@dashboards_app.command("generate")
def dashboards_generate(
    check: bool = typer.Option(False, "--check", help="Fail if committed JSON is stale."),
) -> None:
    """Regenerate Grafana dashboard JSON from tools/dashboards/definitions.py."""
    if check:
        stale = dashboard_generator.stale(paths.DASHBOARDS_DIR)
        if stale:
            raise _fail(f"stale dashboards: {', '.join(stale)} (run `dashboards generate`)")
        console.print("dashboards are up to date")
        return
    changed = dashboard_generator.write_all(paths.DASHBOARDS_DIR)
    console.print(f"updated: {', '.join(changed)}" if changed else "dashboards already up to date")


if __name__ == "__main__":
    app()
