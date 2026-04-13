from __future__ import annotations

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
- Atlassian(Jira, Confluence) 작업에는 `atls` CLI를 사용한다 (mcp-atlassian MCP 대신).
- 사용법: `/atls` 커맨드로 전체 가이드 로드, 또는 `atls <command> --help`.
- 쓰기 작업 전 반드시 `--dry-run` 먼저 실행.
- 기본 출력은 compact (최소 토큰). 본문 필요시 `--format=md`, 파싱시 `--format=json`.
- `--format`은 전역(`atls --format=json ...`) 또는 Jira/Confluence 커맨드 로컬(`... --format=json`)로 둘 다 사용 가능.
- `page push-md` 같은 일부 커맨드는 `-f`를 파일 입력에 사용하므로, 포맷은 긴 플래그 `--format`을 쓴다.
{_ATLS_CLAUDE_BLOCK_END}"""


def _codex_agents_block() -> str:
    """Generate the ATLS block to inject into Codex AGENTS.md."""
    ver = _get_version()
    return f"""{_ATLS_CODEX_BLOCK_START}
<!-- ATLS:VERSION:{ver} -->
## Atlassian via atls
- Jira/Confluence 작업에는 Atlassian MCP 대신 `atls` CLI를 우선 사용한다.
- 자세한 절차, 포맷 선택, 쓰기 안전 규칙이 필요하면 `$atls` skill을 사용한다.
- 쓰기 작업 전에는 반드시 `--dry-run`을 먼저 실행한다.
- 목록/스캔은 compact, 본문 읽기는 `--format=md`, 파싱은 `--format=json`, 원문 보존은 `--format=raw`.
- `--format`은 전역 또는 Jira/Confluence 서브커맨드 로컬로 둘 다 사용할 수 있다.
- `page push-md` 같은 일부 커맨드는 `-f`가 파일 입력이므로 포맷은 `--format` 긴 플래그를 사용한다.
{_ATLS_CODEX_BLOCK_END}"""


def _get_codex_skill_target() -> Path:
    """Primary Codex skill target: ~/.agents/skills/atls/SKILL.md (user-level)."""
    return Path.home() / ".agents" / "skills" / "atls" / "SKILL.md"


def _get_codex_legacy_target() -> Path:
    """Legacy Codex/OMX-compatible skill target kept for compatibility."""
    return Path.home() / ".codex" / "skills" / "atls" / "SKILL.md"


def _get_codex_agents_path() -> Path:
    """User-level Codex global instructions file."""
    return Path.home() / ".codex" / "AGENTS.md"


def _get_claude_target() -> Path:
    """Default Claude command target: ~/.claude/commands/atls.md (user-level)."""
    return Path.home() / ".claude" / "commands" / "atls.md"


def _get_claude_md_path() -> Path:
    """User-level CLAUDE.md."""
    return Path.home() / ".claude" / "CLAUDE.md"


def _install(source: Path, target: Path) -> str:
    """Copy source to target. Backup existing. Return status message."""
    if target.exists():
        existing = target.read_text(encoding="utf-8")
        new_content = source.read_text(encoding="utf-8")
        if existing == new_content:
            return f"  {target}: up-to-date (no-op)"
        backup = target.with_suffix(target.suffix + ".bak")
        shutil.copy2(target, backup)
        target.write_text(new_content, encoding="utf-8")
        return f"  {target}: updated (backup: {backup})"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
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
        if new_content == content:
            return f"  {path}: {label} up-to-date (no-op)"
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


@setup_app.command("codex")
def setup_codex() -> None:
    """Install atls skill for Codex and inject a global AGENTS.md routing block."""
    source_dir = ASSETS_DIR / "codex"
    for msg in _install_tree(source_dir, _get_codex_skill_target().parent):
        typer.echo(msg)
    for msg in _install_tree(source_dir, _get_codex_legacy_target().parent):
        typer.echo(msg)
    typer.echo(_inject_codex_agents_block())


@setup_app.command("claude")
def setup_claude() -> None:
    """Install atls command and CLAUDE.md block for Claude Code."""
    # 1. Install slash command (/atls)
    source = ASSETS_DIR / "claude" / "atls.md"
    msg1 = _install(source, _get_claude_target())
    typer.echo(msg1)

    # 2. Inject ATLS block into ~/.claude/CLAUDE.md
    msg2 = _inject_claude_md_block()
    typer.echo(msg2)


@setup_app.command("all")
def setup_all() -> None:
    """Install skills for both Codex and Claude Code."""
    setup_codex()
    setup_claude()


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
