from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import typer

PACKAGE_NAME = "atlassian-skills"


def _echo_process_output(result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout:
        typer.echo(result.stdout.rstrip())
    if result.stderr:
        typer.echo(result.stderr.rstrip(), err=True)


def _run_checked(command: list[str], *, step_name: str) -> None:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    _echo_process_output(result)
    if result.returncode != 0:
        typer.echo(f"{step_name} failed with exit code {result.returncode}.", err=True)
        raise typer.Exit(result.returncode or 1)


def _require_executable(name: str) -> str:
    path = shutil.which(name)
    if path:
        return path
    typer.echo(f"Required executable `{name}` was not found on PATH.", err=True)
    raise typer.Exit(1)


def _resolve_atls_executable() -> str:
    path = shutil.which("atls")
    if path:
        return path

    current = Path(sys.argv[0]).expanduser()
    if current.exists():
        return str(current)

    typer.echo("Could not locate the refreshed `atls` executable after upgrading.", err=True)
    raise typer.Exit(1)


def upgrade() -> None:
    """Upgrade the uv-installed atls package, then refresh assistant setup assets."""
    uv = _require_executable("uv")

    typer.echo(f"Upgrading `{PACKAGE_NAME}` with `uv tool upgrade`...")
    _run_checked([uv, "tool", "upgrade", PACKAGE_NAME], step_name="uv tool upgrade")

    atls = _resolve_atls_executable()
    typer.echo("Refreshing Claude/Codex setup with `atls setup all`...")
    _run_checked([atls, "setup", "all"], step_name="atls setup all")
