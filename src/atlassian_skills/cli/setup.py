from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import typer

setup_app = typer.Typer(name="setup", help="Install atls skill assets for AI coding assistants")

ASSETS_DIR = Path(__file__).parent.parent / "_assets"

_ATLS_CLAUDE_BLOCK_START = "<!-- ATLS-CLAUDE:START -->"
_ATLS_CLAUDE_BLOCK_END = "<!-- ATLS-CLAUDE:END -->"
_ATLS_CODEX_BLOCK_START = "<!-- ATLS-CODEX:START -->"
_ATLS_CODEX_BLOCK_END = "<!-- ATLS-CODEX:END -->"


# Version is read from the package at runtime
def _get_version() -> str:
    try:
        from atlassian_skills import __version__

        return __version__
    except Exception:
        return "0.1.0"


def _claude_md_block() -> str:
    """Generate the ATLS block to inject into CLAUDE.md."""
    ver = _get_version()
    return f"""{_ATLS_CLAUDE_BLOCK_START}
<!-- ATLS:VERSION:{ver} -->
## Atlassian (atls)
- Atlassian work (Jira/Confluence/Bitbucket/지라/컨플루언스/비트버킷) → load the `atls` skill BEFORE the first atls command.
- This file only routes. Do NOT infer atls flags or syntax from here — the skill is the single source of truth.
{_ATLS_CLAUDE_BLOCK_END}"""


def _codex_agents_block() -> str:
    """Generate the ATLS block to inject into Codex AGENTS.md."""
    ver = _get_version()
    return f"""{_ATLS_CODEX_BLOCK_START}
<!-- ATLS:VERSION:{ver} -->
## Atlassian via atls
- Atlassian work (Jira/Confluence/Bitbucket/지라/컨플루언스/비트버킷) → load the `$atls` skill BEFORE the first atls command.
- This file only routes. Do NOT infer atls flags or syntax from here — the skill is the single source of truth.
{_ATLS_CODEX_BLOCK_END}"""


# Runtime overrides set by _prompt_overrides() — interactive override of env/default paths.
_OVERRIDES: dict[str, Path] = {}


def _detect_platform() -> str:
    """Return a short platform label: 'windows', 'macos', or 'linux'."""
    import platform as _platform

    sys_name = _platform.system().lower()
    if sys_name == "darwin":
        return "macos"
    if sys_name == "windows":
        return "windows"
    return "linux"


def _get_claude_config_dir() -> Path:
    """Claude Code config directory.

    Resolution order:
    1. Interactive override set via `atls setup --interactive`
    2. CLAUDE_CONFIG_DIR environment variable (official)
    3. ~/.claude on all platforms (Windows: %USERPROFILE%\\.claude)
    """
    if "claude" in _OVERRIDES:
        return _OVERRIDES["claude"]
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / ".claude"


def _get_codex_config_dir() -> Path:
    """Codex config directory.

    Resolution order:
    1. Interactive override
    2. CODEX_HOME environment variable
    3. ~/.codex on all platforms
    """
    if "codex" in _OVERRIDES:
        return _OVERRIDES["codex"]
    env_dir = os.environ.get("CODEX_HOME")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / ".codex"


def _get_agents_dir() -> Path:
    """Agents config directory (~/.agents by default)."""
    if "agents" in _OVERRIDES:
        return _OVERRIDES["agents"]
    env_agents = os.environ.get("AGENTS_HOME")
    if env_agents:
        return Path(env_agents).expanduser()
    return Path.home() / ".agents"


def _get_codex_skill_target() -> Path:
    """Canonical Codex skill target: <codex_home>/skills/atls/SKILL.md.

    Codex discovers user-level skills under $CODEX_HOME/skills (default ~/.codex/skills).
    This is the directory shown in Codex's Enable/Disable Skills UI.
    """
    return _get_codex_config_dir() / "skills" / "atls" / "SKILL.md"


def _get_codex_legacy_target() -> Path:
    """Historical legacy Codex skill location (~/.agents/skills/atls/).

    Kept for detection/cleanup only — `atls setup codex` no longer writes here.
    """
    return _get_agents_dir() / "skills" / "atls" / "SKILL.md"


def _get_codex_agents_path() -> Path:
    """User-level Codex global instructions file."""
    return _get_codex_config_dir() / "AGENTS.md"


def _get_claude_skill_target() -> Path:
    """Primary Claude skill target: <config>/skills/atls/SKILL.md (user-level)."""
    return _get_claude_config_dir() / "skills" / "atls" / "SKILL.md"


def _get_claude_command_target() -> Path:
    """Legacy Claude slash command location, kept for detection only."""
    return _get_claude_config_dir() / "commands" / "atls.md"


def _get_claude_md_path() -> Path:
    """User-level CLAUDE.md."""
    return _get_claude_config_dir() / "CLAUDE.md"


def _install(source: Path, target: Path) -> str:
    """Copy source to target. Backup existing. Return status message."""
    new_content = source.read_text(encoding="utf-8")
    if target.exists():
        backup = target.with_suffix(target.suffix + ".bak")
        shutil.copy2(target, backup)
        target.write_text(new_content, encoding="utf-8")
        return f"  {target}: updated (backup: {backup})"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_content, encoding="utf-8")
    return f"  {target}: installed"


def _install_tree(source_dir: Path, target_dir: Path) -> list[str]:
    """Copy an asset tree into the target directory, preserving relative paths."""
    return [
        _install(source_file, target_dir / source_file.relative_to(source_dir))
        for source_file in sorted(source_dir.rglob("*"))
        if source_file.is_file()
    ]


def _inject_marked_block(*, path: Path, start_marker: str, end_marker: str, block: str, label: str) -> str:
    """Inject or replace a marked block in a text file."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(block + "\n", encoding="utf-8")
        return f"  {path}: created with {label}"

    content = path.read_text(encoding="utf-8")
    pattern = re.compile(
        re.escape(start_marker) + r".*?" + re.escape(end_marker),
        re.DOTALL,
    )
    if pattern.search(content):
        new_content = pattern.sub(block, content)
        path.write_text(new_content, encoding="utf-8")
        return f"  {path}: {label} updated"

    separator = (
        "\n\n" if content and not content.endswith("\n\n") else ("\n" if content and not content.endswith("\n") else "")
    )
    path.write_text(content + separator + block + "\n", encoding="utf-8")
    return f"  {path}: {label} appended"


def _inject_claude_md_block() -> str:
    """Inject or replace the ATLS block in ~/.claude/CLAUDE.md."""
    return _inject_marked_block(
        path=_get_claude_md_path(),
        start_marker=_ATLS_CLAUDE_BLOCK_START,
        end_marker=_ATLS_CLAUDE_BLOCK_END,
        block=_claude_md_block(),
        label="ATLS Claude block",
    )


def _inject_codex_agents_block() -> str:
    """Inject or replace the ATLS block in ~/.codex/AGENTS.md."""
    return _inject_marked_block(
        path=_get_codex_agents_path(),
        start_marker=_ATLS_CODEX_BLOCK_START,
        end_marker=_ATLS_CODEX_BLOCK_END,
        block=_codex_agents_block(),
        label="ATLS Codex block",
    )


def _legacy_claude_command_notice() -> str | None:
    """Return a guidance message if a legacy ~/.claude/commands/atls.md is present."""
    path = _get_claude_command_target()
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        content = ""
    skill_path = _get_claude_skill_target()
    if "installed-by: atls" in content:
        return (
            f"  ⚠ Legacy atls slash command found: {path}\n"
            f"    atls now installs as a Claude Skill: {skill_path}\n"
            f"    Remove the old command if no longer needed: rm {path}"
        )
    return (
        f"  ⚠ Custom file at {path} (no installed-by marker).\n"
        f"    atls now installs as a Claude Skill: {skill_path}\n"
        f"    Inspect the file before removing manually."
    )


def _legacy_codex_skill_notice() -> str | None:
    """Return guidance if a legacy ~/.agents/skills/atls install is present.

    atls now installs to the canonical Codex skills dir; a leftover copy under
    ~/.agents/skills makes Codex's Enable/Disable Skills show a duplicate. We warn
    but never auto-delete — the directory may hold files we did not write.
    """
    legacy_dir = _get_codex_legacy_target().parent
    if not legacy_dir.exists():
        return None
    canonical_dir = _get_codex_skill_target().parent
    skill_md = _get_codex_legacy_target()
    try:
        content = skill_md.read_text(encoding="utf-8", errors="replace") if skill_md.exists() else ""
    except OSError:
        content = ""
    if "installed-by: atls" in content:
        return (
            f"  ⚠ Legacy atls skill found: {legacy_dir}\n"
            f"    atls now installs to the canonical Codex skills dir: {canonical_dir}\n"
            f"    Codex may show a duplicate until you remove the old copy: rm -rf {legacy_dir}"
        )
    return (
        f"  ⚠ Skill files at {legacy_dir} (no atls install marker).\n"
        f"    atls now installs to: {canonical_dir}\n"
        f"    Inspect before removing manually."
    )


def _prompt_override(label: str, key: str, current: Path) -> None:
    """Ask the user to accept or override a detected path."""
    source = (
        "override"
        if key in _OVERRIDES
        else (
            "env"
            if os.environ.get(
                {
                    "claude": "CLAUDE_CONFIG_DIR",
                    "codex": "CODEX_HOME",
                    "agents": "AGENTS_HOME",
                }[key]
            )
            else "default"
        )
    )
    typer.echo(f"\n{label}: {current}  (source: {source})")
    entered = typer.prompt(
        "  Press Enter to accept, or paste a custom path",
        default="",
        show_default=False,
    ).strip()
    if entered:
        _OVERRIDES[key] = Path(entered).expanduser()
        typer.echo(f"  → using: {_OVERRIDES[key]}")


def _prompt_all_overrides() -> None:
    """Interactive walkthrough of all configurable paths."""
    typer.echo(f"Detected platform: {_detect_platform()}")
    _prompt_override("Claude config dir", "claude", _get_claude_config_dir())
    _prompt_override("Codex config dir (canonical skill target)", "codex", _get_codex_config_dir())
    _prompt_override("Agents dir (legacy Codex skill, detection only)", "agents", _get_agents_dir())
    typer.echo("")


def _show_paths() -> None:
    """Print all resolved install paths without running setup."""
    typer.echo(f"Platform: {_detect_platform()}")
    typer.echo(f"  Claude config dir         : {_get_claude_config_dir()}")
    typer.echo(f"  Claude skill target       : {_get_claude_skill_target()}")
    typer.echo(f"  Claude command (legacy)   : {_get_claude_command_target()}")
    typer.echo(f"  CLAUDE.md path            : {_get_claude_md_path()}")
    typer.echo(f"  Codex config dir          : {_get_codex_config_dir()}")
    typer.echo(f"  Codex AGENTS.md path      : {_get_codex_agents_path()}")
    typer.echo(f"  Codex skill target        : {_get_codex_skill_target()}  (canonical)")
    typer.echo(f"  Codex legacy skill target : {_get_codex_legacy_target()}  (detection only)")
    typer.echo("")
    typer.echo("Override via environment variables:")
    typer.echo("  CLAUDE_CONFIG_DIR  — Claude Code config directory")
    typer.echo("  CODEX_HOME         — Codex config directory (canonical skills live here)")
    typer.echo("  AGENTS_HOME        — legacy Codex skill directory (detection only)")
    typer.echo("Or run `atls setup <target> --interactive` to override at install time.")


_CANONICAL_SKILL_DIR = ASSETS_DIR / "skills" / "atls"


@setup_app.command("codex")
def setup_codex(
    interactive: bool = typer.Option(False, "--interactive", "-i", help="Prompt for path overrides"),
) -> None:
    """Install atls skill for Codex and inject a global AGENTS.md routing block."""
    if interactive:
        _prompt_all_overrides()
    # Install only to the canonical Codex skills dir ($CODEX_HOME/skills, default ~/.codex/skills).
    for msg in _install_tree(_CANONICAL_SKILL_DIR, _get_codex_skill_target().parent):
        typer.echo(msg)
    typer.echo(_inject_codex_agents_block())
    # Warn about (never auto-delete) a leftover legacy ~/.agents/skills install.
    legacy = _legacy_codex_skill_notice()
    if legacy:
        typer.echo(legacy)


@setup_app.command("claude")
def setup_claude(
    interactive: bool = typer.Option(False, "--interactive", "-i", help="Prompt for path overrides"),
) -> None:
    """Install atls skill for Claude Code and inject a CLAUDE.md routing block."""
    if interactive:
        _prompt_all_overrides()
    # 1. Install Claude Skill (~/.claude/skills/atls/)
    for msg in _install_tree(_CANONICAL_SKILL_DIR, _get_claude_skill_target().parent):
        typer.echo(msg)

    # 2. Inject ATLS block into the resolved CLAUDE.md
    typer.echo(_inject_claude_md_block())

    # 3. Warn about any legacy slash command (do not delete automatically)
    legacy = _legacy_claude_command_notice()
    if legacy:
        typer.echo(legacy)


@setup_app.command("all")
def setup_all(
    interactive: bool = typer.Option(False, "--interactive", "-i", help="Prompt for path overrides"),
) -> None:
    """Install skills for both Codex and Claude Code."""
    if interactive:
        _prompt_all_overrides()
    setup_codex(interactive=False)  # overrides already set
    setup_claude(interactive=False)


@setup_app.command("paths")
def setup_paths() -> None:
    """Show all resolved install paths (without installing anything)."""
    _show_paths()


@setup_app.command("status")
def setup_status() -> None:
    """Check installation status."""
    for name, target in [
        ("Claude skill", _get_claude_skill_target()),
        ("Claude command (legacy)", _get_claude_command_target()),
        ("Codex skill", _get_codex_skill_target()),
        ("Codex legacy skill", _get_codex_legacy_target()),
    ]:
        if target.exists():
            content = target.read_text(encoding="utf-8")
            if "installed-by: atls" in content:
                m = re.search(r"installed-by: atls (\S+)", content)
                ver = m.group(1) if m else "unknown"
                typer.echo(f"  {name}: installed (v{ver}) at {target}")
            else:
                typer.echo(f"  {name}: found at {target} (no version marker)")
        else:
            typer.echo(f"  {name}: not installed ({target})")

    legacy = _legacy_claude_command_notice()
    if legacy:
        typer.echo(legacy)

    legacy_codex = _legacy_codex_skill_notice()
    if legacy_codex:
        typer.echo(legacy_codex)

    for name, path in [("Codex AGENTS.md", _get_codex_agents_path()), ("CLAUDE.md", _get_claude_md_path())]:
        if path.exists():
            content = path.read_text(encoding="utf-8")
            m = re.search(r"ATLS:VERSION:(\S+)", content)
            if m:
                typer.echo(f"  {name}: ATLS block v{m.group(1)} at {path}")
            else:
                typer.echo(f"  {name}: no ATLS block at {path}")
        else:
            typer.echo(f"  {name}: not found ({path})")
