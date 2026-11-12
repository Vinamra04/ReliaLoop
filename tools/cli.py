"""ReliaLoop command line: the canonical interface to the whole project.

The Makefile is only a thin wrapper over these commands, so everything works
on machines without `make` (native Windows). Commands are added phase by
phase; `--help` always lists exactly what exists.
"""

from __future__ import annotations

import typer
from rich.console import Console

from tools import init_env, paths

app = typer.Typer(
    name="relialoop",
    help="ReliaLoop: SLOs as code, alerting, chaos and an evaluated AIOps agent.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.callback()
def main() -> None:
    """ReliaLoop: SLOs as code, alerting, chaos and an evaluated AIOps agent."""


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


if __name__ == "__main__":
    app()
