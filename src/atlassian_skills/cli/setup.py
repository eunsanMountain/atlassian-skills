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
- Use `atls` CLI for all Atlassian (Jira, Confluence) operations instead of mcp-atlassian MCP.
- Usage: load the full guide with `/atls`, or run `atls <command> --help`.
- Always run `--dry-run` before write operations.
- Default output is compact (minimal tokens). Use `--format=md` for body reading, `--format=json` for parsing.
- `--format` works both globally (`atls --format=json ...`) and locally on subcommands (`... --format=json`).
- Some commands use `-f` for file input (e.g. `page push-md`), so use the long form `--format` for output format.
{_ATLS_CLAUDE_BLOCK_END}"""


def _codex_agents_block() -> str:
    """Generate the ATLS block to inject into Codex AGENTS.md."""
    ver = _get_version()
    return f"""{_ATLS_CODEX_BLOCK_START}
<!-- ATLS:VERSION:{ver} -->
## Atlassian via atls
- Prefer `atls` CLI over Atlassian MCP tools for all Jira/Confluence operations.
- Use the `$atls` skill for detailed format selection, write safety rules, and workflows.
- Always run `--dry-run` before write operations.
- Listing/scanning: compact (default). Body reading: `--format=md`. Parsing: `--format=json`. Raw: `--format=raw`.
- `--format` works both globally and locally on Jira/Confluence subcommands.
- Some commands use `-f` for file input (e.g. `page push-md`), so use the long form `--format` for output format.
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
    """Primary Codex skill target: <agents_dir>/skills/atls/SKILL.md (user-level)."""
    return _get_agents_dir() / "skills" / "atls" / "SKILL.md"


def _get_codex_legacy_target() -> Path:
    """Legacy Codex/OMX-compatible skill target kept for compatibility."""
    return _get_codex_config_dir() / "skills" / "atls" / "SKILL.md"


def _get_codex_agents_path() -> Path:
    """User-level Codex global instructions file."""
    return _get_codex_config_dir() / "AGENTS.md"


def _get_claude_target() -> Path:
    """Default Claude command target: <config>/commands/atls.md (user-level)."""
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

    separator = "\n\n" if content and not content.endswith("\n\n") else ("\n" if content and not content.endswith("\n") else "")
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


def _prompt_override(label: str, key: str, current: Path) -> None:
    """Ask the user to accept or override a detected path."""
    source = "override" if key in _OVERRIDES else (
        "env" if os.environ.get({
            "claude": "CLAUDE_CONFIG_DIR",
            "codex": "CODEX_HOME",
            "agents": "AGENTS_HOME",
        }[key]) else "default"
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
    _prompt_override("Codex config dir", "codex", _get_codex_config_dir())
    _prompt_override("Agents dir (Codex skill target)", "agents", _get_agents_dir())
    typer.echo("")


def _show_paths() -> None:
    """Print all resolved install paths without running setup."""
    typer.echo(f"Platform: {_detect_platform()}")
    typer.echo(f"  Claude config dir      : {_get_claude_config_dir()}")
    typer.echo(f"  Claude command target  : {_get_claude_target()}")
    typer.echo(f"  CLAUDE.md path         : {_get_claude_md_path()}")
    typer.echo(f"  Codex config dir       : {_get_codex_config_dir()}")
    typer.echo(f"  Codex AGENTS.md path   : {_get_codex_agents_path()}")
    typer.echo(f"  Codex skill target     : {_get_codex_skill_target()}")
    typer.echo(f"  Codex legacy target    : {_get_codex_legacy_target()}")
    typer.echo("")
    typer.echo("Override via environment variables:")
    typer.echo("  CLAUDE_CONFIG_DIR  — Claude Code config directory")
    typer.echo("  CODEX_HOME         — Codex config directory")
    typer.echo("  AGENTS_HOME        — Agents skill directory")
    typer.echo("Or run `atls setup <target> --interactive` to override at install time.")


@setup_app.command("codex")
def setup_codex(
    interactive: bool = typer.Option(False, "--interactive", "-i", help="Prompt for path overrides"),
) -> None:
    """Install atls skill for Codex and inject a global AGENTS.md routing block."""
    if interactive:
        _prompt_all_overrides()
    source_dir = ASSETS_DIR / "codex"
    for msg in _install_tree(source_dir, _get_codex_skill_target().parent):
        typer.echo(msg)
    for msg in _install_tree(source_dir, _get_codex_legacy_target().parent):
        typer.echo(msg)
    typer.echo(_inject_codex_agents_block())


@setup_app.command("claude")
def setup_claude(
    interactive: bool = typer.Option(False, "--interactive", "-i", help="Prompt for path overrides"),
) -> None:
    """Install atls command and CLAUDE.md block for Claude Code."""
    if interactive:
        _prompt_all_overrides()
    # 1. Install slash command (/atls)
    source = ASSETS_DIR / "claude" / "atls.md"
    msg1 = _install(source, _get_claude_target())
    typer.echo(msg1)

    # 2. Inject ATLS block into the resolved CLAUDE.md
    msg2 = _inject_claude_md_block()
    typer.echo(msg2)


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
        ("Codex skill", _get_codex_skill_target()),
        ("Codex legacy skill", _get_codex_legacy_target()),
        ("Claude command", _get_claude_target()),
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
