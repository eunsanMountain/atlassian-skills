from __future__ import annotations

import re
from pathlib import Path

import typer

from atlassian_skills.cli.auth import render_auth_status
from atlassian_skills.cli.setup import (
    _detect_platform,
    _detect_shell,
    _get_claude_command_target,
    _get_claude_config_dir,
    _get_claude_md_path,
    _get_claude_skill_target,
    _get_codex_agents_path,
    _get_codex_config_dir,
    _get_codex_legacy_target,
    _get_codex_skill_target,
    _get_copilot_config_dir,
    _get_copilot_instructions_path,
    _get_copilot_skill_target,
    _is_fish,
    _is_git_bash,
    _legacy_claude_command_notice,
    _legacy_codex_skill_notice,
)


def _print_skill_status(label: str, target: Path, hide_when_absent: bool = False) -> None:
    """Print one skill install line — version marker if present, else 'not installed'.

    `hide_when_absent=True` keeps legacy paths off the output for the 95% of users who
    installed cleanly post-0.2.5; they only matter when the file actually exists, in
    which case the legacy-notice helpers (`_legacy_*_notice`) describe the migration.
    """
    if not target.exists():
        if hide_when_absent:
            return
        typer.echo(f"  {label}: not installed ({target})")
        return
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        typer.echo(f"  {label}: present at {target} (could not read)")
        return
    if "installed-by: atls" in content:
        m = re.search(r"installed-by: atls (\S+)", content)
        ver = m.group(1) if m else "unknown"
        typer.echo(f"  {label}: installed (v{ver}) at {target}")
    else:
        typer.echo(f"  {label}: found at {target} (no version marker)")


def _print_routing_block_version(label: str, path: Path) -> None:
    if not path.exists():
        typer.echo(f"  {label}: not found ({path})")
        return
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        typer.echo(f"  {label}: present at {path} (could not read)")
        return
    m = re.search(r"ATLS:VERSION:(\S+)", content)
    if m:
        typer.echo(f"  {label}: ATLS block v{m.group(1)} at {path}")
    else:
        typer.echo(f"  {label}: no ATLS block at {path}")


def doctor() -> None:
    """Diagnose atls installation: platform, paths, skill status, auth resolution."""
    platform_name = _detect_platform()
    shell = _detect_shell()
    typer.echo(f"Platform: {platform_name} (shell: {shell})")
    if _is_fish():
        typer.echo(
            "  Note: fish shell is detected — `atls setup` wizard refuses to run here.\n"
            "        Use `atls setup --skills-only` for skill assets, and configure env vars manually."
        )
    if _is_git_bash():
        typer.echo("  Note: Git Bash detected — env vars are still read from HKCU\\Environment.")
    typer.echo("")

    typer.echo("Paths:")
    typer.echo(f"  Claude config dir   : {_get_claude_config_dir()}")
    typer.echo(f"  Claude skill target : {_get_claude_skill_target()}")
    typer.echo(f"  CLAUDE.md path      : {_get_claude_md_path()}")
    typer.echo(f"  Codex config dir    : {_get_codex_config_dir()}")
    typer.echo(f"  Codex AGENTS.md path: {_get_codex_agents_path()}")
    typer.echo(f"  Codex skill target  : {_get_codex_skill_target()}  (canonical)")
    typer.echo(f"  Copilot config dir  : {_get_copilot_config_dir()}")
    typer.echo(f"  Copilot skill target: {_get_copilot_skill_target()}")
    typer.echo(f"  Copilot instructions: {_get_copilot_instructions_path()}")
    if _get_claude_command_target().exists() or _get_codex_legacy_target().exists():
        typer.echo("  legacy paths (only shown when present):")
        if _get_claude_command_target().exists():
            typer.echo(f"    Claude command (legacy)   : {_get_claude_command_target()}")
        if _get_codex_legacy_target().exists():
            typer.echo(f"    Codex legacy skill target : {_get_codex_legacy_target()}")
    typer.echo("")

    typer.echo("Skill installation status:")
    _print_skill_status("Claude skill", _get_claude_skill_target())
    _print_skill_status("Claude command (legacy)", _get_claude_command_target(), hide_when_absent=True)
    _print_skill_status("Codex skill", _get_codex_skill_target())
    _print_skill_status("Codex legacy skill", _get_codex_legacy_target(), hide_when_absent=True)
    _print_skill_status("Copilot skill", _get_copilot_skill_target())

    legacy_cmd = _legacy_claude_command_notice()
    if legacy_cmd:
        typer.echo(legacy_cmd)
    legacy_codex = _legacy_codex_skill_notice()
    if legacy_codex:
        typer.echo(legacy_codex)

    _print_routing_block_version("Codex AGENTS.md", _get_codex_agents_path())
    _print_routing_block_version("CLAUDE.md", _get_claude_md_path())
    _print_routing_block_version("Copilot instructions", _get_copilot_instructions_path())
    typer.echo("")

    typer.echo("Auth:")
    render_auth_status("default")
