from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

import typer

PACKAGE_NAME = "atlassian-skills"


def _detect_install_method() -> Literal["uv", "pipx", "pip"]:
    """Return the tool that installed atls, based on `sys.executable` layout.

    - uv tool:  `<data>/uv/tools/<package>/(bin|Scripts)/python`
    - pipx:     `<data>/pipx/venvs/<package>/(bin|Scripts)/python`
    - pip:      anything else (system pip, pip inside a user-managed venv, ...)

    The check is case-insensitive and works identically on Windows, macOS, and Linux
    because `pathlib` normalises path separators before splitting into parts.
    """
    parts = [p.lower() for p in Path(sys.executable).resolve().parts]
    for i in range(len(parts) - 1):
        if parts[i] == "uv" and parts[i + 1] == "tools":
            return "uv"
        if parts[i] == "pipx" and parts[i + 1] == "venvs":
            return "pipx"
    return "pip"


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
    """Upgrade the atls package (auto-detects uv/pipx/pip), then refresh assistant setup assets."""
    method = _detect_install_method()

    if method == "uv":
        uv = _require_executable("uv")
        typer.echo(f"Upgrading `{PACKAGE_NAME}` with `uv tool upgrade` (detected uv install)...")
        _run_checked([uv, "tool", "upgrade", PACKAGE_NAME], step_name="uv tool upgrade")
    elif method == "pipx":
        pipx = shutil.which("pipx")
        if pipx:
            typer.echo(f"Upgrading `{PACKAGE_NAME}` with `pipx upgrade` (detected pipx install)...")
            _run_checked([pipx, "upgrade", PACKAGE_NAME], step_name="pipx upgrade")
        else:
            typer.echo(
                "Detected a pipx install but `pipx` is not on PATH — falling back to "
                "`pip install --upgrade` inside the pipx venv.",
            )
            _run_checked(
                [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE_NAME],
                step_name="pip install --upgrade",
            )
    else:
        typer.echo(f"Upgrading `{PACKAGE_NAME}` with `pip install --upgrade` (detected pip install)...")
        typer.echo(
            "Hint: consider reinstalling via `uv tool install atlassian-skills` or "
            "`pipx install atlassian-skills` for isolated, reproducible upgrades.",
        )
        _run_checked(
            [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE_NAME],
            step_name="pip install --upgrade",
        )

    atls = _resolve_atls_executable()
    typer.echo("Refreshing Claude/Codex setup with `atls setup all`...")
    _run_checked([atls, "setup", "all"], step_name="atls setup all")
