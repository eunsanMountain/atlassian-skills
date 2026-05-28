from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

import typer

setup_app = typer.Typer(
    name="setup",
    help="Interactive wizard: configure Atlassian URLs, tokens, and AI agent skills in one pass.",
)

ASSETS_DIR = Path(__file__).parent.parent / "_assets"

_ATLS_CLAUDE_BLOCK_START = "<!-- ATLS-CLAUDE:START -->"
_ATLS_CLAUDE_BLOCK_END = "<!-- ATLS-CLAUDE:END -->"
_ATLS_CODEX_BLOCK_START = "<!-- ATLS-CODEX:START -->"
_ATLS_CODEX_BLOCK_END = "<!-- ATLS-CODEX:END -->"
_ATLS_COPILOT_BLOCK_START = "<!-- ATLS-COPILOT:START -->"
_ATLS_COPILOT_BLOCK_END = "<!-- ATLS-COPILOT:END -->"

_SHELL_RC_BLOCK_START = "# >>> atls env >>>"
_SHELL_RC_BLOCK_END = "# <<< atls env <<<"

# Reuse the single source of truth for legacy token env-var names. core/config.py
# uses this same dict for `get_env_token` fallback lookup; importing here keeps the
# two ends (wizard writes / auth reads) in lockstep.
from atlassian_skills.core.config import _LEGACY_TOKEN_VARS as _TOKEN_ENV_NAMES  # noqa: E402, PLC0415

_PRODUCTS = ("jira", "confluence", "bitbucket")

_AGENT_WARNING = "⚠ Run this directly in a real terminal — never through an AI agent."


# ---------------------------------------------------------------------------
# Version + boilerplate (boring helpers retained for shim backward-compat)
# ---------------------------------------------------------------------------


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


def _copilot_instructions_block() -> str:
    """Generate the ATLS block to inject into Copilot's copilot-instructions.md.

    Copilot CLI does not have a magic skill-loader directive like Codex's `$atls` —
    it reads `copilot-instructions.md` as plain global guidance. The text below tells
    Copilot to defer to the SKILL.md content rather than fabricate atls syntax.
    """
    ver = _get_version()
    return f"""{_ATLS_COPILOT_BLOCK_START}
<!-- ATLS:VERSION:{ver} -->
## Atlassian via atls
- Atlassian work (Jira/Confluence/Bitbucket/지라/컨플루언스/비트버킷) → read the `atls` skill at `~/.copilot/skills/atls/SKILL.md` BEFORE the first atls command.
- This file only routes. Do NOT infer atls flags or syntax from here — the skill is the single source of truth.
{_ATLS_COPILOT_BLOCK_END}"""


# ---------------------------------------------------------------------------
# Platform / shell detection
# ---------------------------------------------------------------------------


def _detect_platform() -> str:
    """Return a short platform label: 'windows', 'macos', or 'linux'."""
    import platform as _platform

    sys_name = _platform.system().lower()
    if sys_name == "darwin":
        return "macos"
    if sys_name == "windows":
        return "windows"
    return "linux"


def _is_git_bash() -> bool:
    """Detect Git Bash (MINGW64/MINGW32) on Windows."""
    return os.environ.get("MSYSTEM", "").startswith("MINGW")


def _is_fish() -> bool:
    """Detect fish shell. fish writes a different `set -gx` syntax — wizard refuses."""
    if os.environ.get("FISH_VERSION"):
        return True
    shell = os.environ.get("SHELL", "")
    return shell.endswith("/fish")


def _is_wsl() -> bool:
    """Detect Windows Subsystem for Linux. `~/.copilot` here lives in the WSL filesystem,
    invisible to a native Windows Copilot CLI install — worth warning the user."""
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


def _detect_shell() -> str:
    """Return the active shell label for display + rc-path resolution."""
    if sys.platform == "win32":
        if _is_git_bash():
            return "gitbash"
        if os.environ.get("PSModulePath"):  # noqa: SIM112 — Windows env var preserves canonical PowerShell casing
            return "powershell"
        return "cmd"
    if _is_fish():
        return "fish"
    shell = os.environ.get("SHELL", "")
    if shell.endswith("/zsh"):
        return "zsh"
    if shell.endswith("/bash"):
        return "bash"
    return "sh"


def _shell_rc_path() -> Path:
    """Return the rc file the wizard appends an env block to.

    Windows is handled by `_save_tokens_windows` instead — this is Unix-only.
    fish is rejected upstream by `_is_fish` so we never reach this with shell='fish'.
    """
    shell = _detect_shell()
    home = Path.home()
    if shell == "zsh":
        return home / ".zshrc"
    if shell == "bash":
        return home / ".bashrc"
    return home / ".profile"  # POSIX fallback for unknown shells


# ---------------------------------------------------------------------------
# Path resolution (Claude / Codex / Agents)
# ---------------------------------------------------------------------------


def _get_claude_config_dir() -> Path:
    """Claude Code config directory. Resolution: CLAUDE_CONFIG_DIR > ~/.claude."""
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / ".claude"


def _get_codex_config_dir() -> Path:
    """Codex config directory. Resolution: CODEX_HOME > ~/.codex."""
    env_dir = os.environ.get("CODEX_HOME")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / ".codex"


def _get_copilot_config_dir() -> Path:
    """GitHub Copilot skills directory. Resolution: COPILOT_HOME > ~/.copilot.

    Per the Copilot Skills documentation, Copilot scans `~/.copilot/skills` and
    `~/.agents/skills` for personal skills. We target the canonical `~/.copilot`
    location; users who prefer the shared `~/.agents` tree can set COPILOT_HOME.
    """
    env_dir = os.environ.get("COPILOT_HOME")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / ".copilot"


def _get_agents_dir() -> Path:
    """Legacy ~/.agents directory (detection only)."""
    env_agents = os.environ.get("AGENTS_HOME")
    if env_agents:
        return Path(env_agents).expanduser()
    return Path.home() / ".agents"


def _get_codex_skill_target() -> Path:
    """Canonical Codex skill target: <codex_home>/skills/atls/SKILL.md."""
    return _get_codex_config_dir() / "skills" / "atls" / "SKILL.md"


def _get_copilot_skill_target() -> Path:
    """Canonical GitHub Copilot skill target: <copilot_home>/skills/atls/SKILL.md."""
    return _get_copilot_config_dir() / "skills" / "atls" / "SKILL.md"


def _get_copilot_instructions_path() -> Path:
    """GitHub Copilot CLI's user-global instructions file.

    Per https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions,
    Copilot CLI reads `$HOME/.copilot/copilot-instructions.md`. Equivalent to
    Claude's `CLAUDE.md` and Codex's `AGENTS.md` for routing-block injection.
    """
    return _get_copilot_config_dir() / "copilot-instructions.md"


def _get_codex_legacy_target() -> Path:
    """Historical legacy Codex skill location (~/.agents/skills/atls/)."""
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


# ---------------------------------------------------------------------------
# File install + marker block injection (legacy helpers — reused by shim)
# ---------------------------------------------------------------------------


def _install(source: Path, target: Path) -> str:
    """Copy source to target. Backup existing. Return status message.

    Short-circuits when source bytes equal target bytes — avoids .bak churn on every
    `atls upgrade` (which silently runs `setup --skills-only` after each upgrade,
    even when SKILL.md content hasn't changed across the patch release).
    """
    new_content = source.read_text(encoding="utf-8")
    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8")
            if existing == new_content:
                return f"  {target}: unchanged"
        except OSError:
            pass
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


def _inject_copilot_instructions_block() -> str:
    """Inject or replace the ATLS block in ~/.copilot/copilot-instructions.md."""
    return _inject_marked_block(
        path=_get_copilot_instructions_path(),
        start_marker=_ATLS_COPILOT_BLOCK_START,
        end_marker=_ATLS_COPILOT_BLOCK_END,
        block=_copilot_instructions_block(),
        label="ATLS Copilot block",
    )


def _legacy_claude_command_notice() -> str | None:
    """Return guidance if a legacy ~/.claude/commands/atls.md is present."""
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

    Skips empty directories (e.g. the user manually `rm`'d SKILL.md but never `rmdir`'d) —
    those produced spurious migration nags every doctor/wizard run.
    """
    legacy_dir = _get_codex_legacy_target().parent
    if not legacy_dir.exists():
        return None
    skill_md = _get_codex_legacy_target()
    # Empty / no-SKILL.md directory: nothing to migrate, suppress the warning.
    if not skill_md.exists():
        return None
    canonical_dir = _get_codex_skill_target().parent
    try:
        content = skill_md.read_text(encoding="utf-8", errors="replace")
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


_CANONICAL_SKILL_DIR = ASSETS_DIR / "skills" / "atls"


# ---------------------------------------------------------------------------
# Shim deprecation warning (0.2.7 → 0.3.0 migration aid)
# ---------------------------------------------------------------------------


def _emit_deprecation(command: str) -> None:
    """Stderr-only deprecation warning for legacy `setup <command>` calls."""
    typer.echo(
        f"⚠ deprecated: 'atls setup {command}' will be removed in 0.3.0. Use 'atls setup' (wizard) instead.",
        err=True,
    )


# ---------------------------------------------------------------------------
# Token storage — Unix (file-based) + Windows (winreg)
# ---------------------------------------------------------------------------


def _secrets_dir() -> Path:
    """Standard file-based secrets directory."""
    return Path.home() / ".secrets"


def _save_token_unix(product: str, token: str) -> Path:
    """Write ~/.secrets/{product}_pat (mode 0o600) AND update current process env.

    Updating `os.environ` is critical — the wizard's final `auth_status` verification
    runs in the same Python process, before any shell `source ~/.zshrc`. Without this,
    the freshly-saved token wouldn't be visible to the verification step.

    Uses tempfile + atomic `os.replace` so the new token never sits at a wider mode:
    `os.open(O_CREAT, 0o600)` would inherit a pre-existing file's mode until the
    trailing `os.chmod` ran, leaving a brief window where the new token is readable
    on a shared host.
    """
    import contextlib
    import tempfile

    secrets = _secrets_dir()
    secrets.mkdir(mode=0o700, exist_ok=True)
    path = secrets / f"{product}_pat"

    fd, tmp_str = tempfile.mkstemp(dir=str(secrets), prefix=f".{product}_pat.", text=False)
    tmp_path = Path(tmp_str)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
        os.replace(tmp_path, path)  # atomic on POSIX
    except Exception:
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise
    # Same-process env update — see docstring.
    os.environ[_TOKEN_ENV_NAMES[product]] = token
    return path


def _build_shell_env_block(secrets_paths: dict[str, Path]) -> str:
    """Build an idempotent bash/zsh-compatible source block for secrets files."""
    home = Path.home()
    lines = [_SHELL_RC_BLOCK_START]
    for product in _PRODUCTS:
        if product not in secrets_paths:
            continue
        path = secrets_paths[product]
        try:
            rel = path.relative_to(home)
            disp = f"~/{rel}"
        except ValueError:
            disp = str(path)
        env_name = _TOKEN_ENV_NAMES[product]
        lines.append(f'[ -f {disp} ] && export {env_name}="$(cat {disp})"')
    lines.append(_SHELL_RC_BLOCK_END)
    return "\n".join(lines)


def _inject_shell_env_block(secrets_paths: dict[str, Path]) -> str:
    """Append/replace the atls env block in ~/.zshrc or ~/.bashrc."""
    rc = _shell_rc_path()
    block = _build_shell_env_block(secrets_paths)
    return _inject_marked_block(
        path=rc,
        start_marker=_SHELL_RC_BLOCK_START,
        end_marker=_SHELL_RC_BLOCK_END,
        block=block,
        label="ATLS env block",
    )


def _save_tokens_windows(env_vars: dict[str, str]) -> None:
    """Write env vars to HKCU\\Environment, broadcast WM_SETTINGCHANGE, update os.environ.

    Works identically from cmd, PowerShell 5/7, and Git Bash — the registry key is the
    same single source of truth for user-scoped env vars on Windows.
    """
    if sys.platform != "win32":
        return
    import ctypes
    import winreg  # type: ignore[import-not-found]

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        for name, value in env_vars.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    # Best-effort broadcast so newly-spawned processes pick up the change. Already-running
    # shells won't see it — they snapshot the env block at process start.
    hwnd_broadcast = 0xFFFF
    wm_settingchange = 0x001A
    smto_abortifhung = 0x0002
    try:
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
            hwnd_broadcast,
            wm_settingchange,
            0,
            ctypes.c_wchar_p("Environment"),
            smto_abortifhung,
            5000,
            ctypes.byref(result),
        )
    except Exception:
        pass

    # Same-process env update so the verification step sees the new values.
    for name, value in env_vars.items():
        os.environ[name] = value


# ---------------------------------------------------------------------------
# Wizard state — URL/token resolution
# ---------------------------------------------------------------------------


def _existing_url_state(profile_name: str = "default") -> dict[str, tuple[str | None, str | None]]:
    """Resolve all product URLs in one shot. Loads `config.toml` once, not per product."""
    from atlassian_skills.cli.auth import _resolve_url
    from atlassian_skills.core.config import get_profile, load_config

    prof = get_profile(load_config(), profile_name)
    url_fields = {
        "jira": prof.jira_url,
        "confluence": prof.confluence_url,
        "bitbucket": prof.bitbucket_url,
    }
    return {p: _resolve_url(profile_name, p, url_fields[p]) for p in _PRODUCTS}


def _existing_tokens(profile_name: str = "default") -> dict[str, int]:
    """Return token lengths (never raw values) — **env vars only**.

    `~/.secrets/*_pat` files are *not* a fallback here. The user must source their shell
    rc (or open a new terminal) for the wizard-managed tokens to become visible; reading
    the file directly would falsely report tokens as 'set' for users who intentionally
    removed the export line from their rc. File/env mismatch is surfaced separately as a
    one-line banner by `_orphan_token_files()`.
    """
    from atlassian_skills.core.config import get_env_token

    out: dict[str, int] = {}
    for p in _PRODUCTS:
        token = get_env_token(profile_name, p)
        out[p] = len(token) if token else 0
    return out


def _orphan_token_files() -> list[str]:
    """Return product names whose ~/.secrets/{p}_pat exists but no env var is loaded.

    Used to print an informational banner: the user likely needs `source ~/.zshrc` to
    pick the tokens up, or wants to delete a stale file.
    """
    from atlassian_skills.core.config import get_env_token

    orphans: list[str] = []
    secrets = _secrets_dir()
    for p in _PRODUCTS:
        if get_env_token("default", p):
            continue
        if (secrets / f"{p}_pat").exists():
            orphans.append(p)
    return orphans


def _existing_file_state() -> dict[str, bool]:
    """Return whether each product has a `~/.secrets/{product}_pat` on disk.

    Wizard uses this so the [k/e/r/s] menu (which exposes the [r]emove action that the
    orphan banner promises) is reachable even when there's no URL and no env var loaded.
    """
    secrets = _secrets_dir()
    return {p: (secrets / f"{p}_pat").exists() for p in _PRODUCTS}


def _detect_atls_default_shadowing(saved_products: list[str]) -> list[str]:
    """Warn when the wizard wrote `JIRA_PERSONAL_TOKEN` etc. but `ATLS_DEFAULT_*_TOKEN`
    is also set in env. `get_env_token` prefers the ATLS_* form, so the wizard's value
    is silently overridden in every subsequent `atls` invocation.
    """
    warnings: list[str] = []
    for product in saved_products:
        atls_var = f"ATLS_DEFAULT_{product.upper()}_TOKEN"
        if os.environ.get(atls_var):
            warnings.append(
                f"{atls_var} is set in your environment and takes priority over the "
                f"wizard-managed {_TOKEN_ENV_NAMES[product]}. The new token won't be "
                f"used until you `unset {atls_var}` (or remove it from your shell rc)."
            )
    return warnings


def _existing_secrets_paths() -> dict[str, Path]:
    """Return `~/.secrets/{product}_pat` paths that actually exist on disk."""
    secrets = _secrets_dir()
    out: dict[str, Path] = {}
    for p in _PRODUCTS:
        candidate = secrets / f"{p}_pat"
        if candidate.exists():
            out[p] = candidate
    return out


def _inject_shell_env_block_from_disk() -> str:
    """Rebuild the shell rc block from ALL existing ~/.secrets/*_pat files on disk.

    Prevents the v4 bug where setting a fresh Bitbucket token wiped the previously-saved
    Jira/Confluence exports — the previous version rebuilt the block from in-memory new
    tokens only, so any product not touched in this wizard run lost its export line.
    """
    paths = _existing_secrets_paths()
    if not paths:
        return f"  no token files in {_secrets_dir()} — shell rc block left unchanged"
    return _inject_shell_env_block(paths)


def _detect_rc_shadowing() -> list[str]:
    """Warn if a token env var is exported manually outside the atls-managed block.

    Catches the common foot-gun where a user kept an old `export JIRA_PERSONAL_TOKEN=…`
    line they wrote by hand. Depending on its position relative to the atls block, it
    can silently override the wizard-managed value.

    ATLS_DEFAULT_*_TOKEN shadowing is intentionally NOT checked here — that's an
    advanced multi-profile setup and documented in the README priority table.
    """
    warnings: list[str] = []
    if sys.platform == "win32":
        return warnings
    rc = _shell_rc_path()
    if not rc.exists():
        return warnings
    try:
        content = rc.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return warnings
    # Strip the atls-managed block so we only inspect user-authored lines
    pattern = re.compile(
        re.escape(_SHELL_RC_BLOCK_START) + r".*?" + re.escape(_SHELL_RC_BLOCK_END),
        re.DOTALL,
    )
    stripped = pattern.sub("", content)
    for env_name in _TOKEN_ENV_NAMES.values():
        if re.search(rf"^\s*export\s+{re.escape(env_name)}\s*=", stripped, re.MULTILINE):
            warnings.append(
                f"{env_name} is also exported in {rc} outside the atls block. "
                "Depending on line order this may override the wizard-managed token — "
                "consider removing the manual export.\n"
                "  (False positive? This is a plain regex scan — if the match is inside "
                "a heredoc, function body, or quoted string that doesn't run at shell init, ignore.)"
            )
    return warnings


def _pat_issuer_hint(product: str) -> str:
    """Where to generate the PAT — generic instruction (no URL guessing).

    Personal-access-token paths vary across Atlassian Server/DC versions and
    custom deployments (`/plugins/personalaccesstokens/...`, `/secure/ViewProfile.jspa`,
    `/account`, etc.). A guessed URL that returns 404 is worse than a clear
    written instruction the user can find in the UI.
    """
    if product == "bitbucket":
        return "In Bitbucket: Profile → Manage Account → HTTP access tokens → Create"
    return f"In {product.capitalize()}: Profile (top-right avatar) → Personal Access Tokens → Create"


def _apply_url_changes(url_actions: dict[str, tuple[str | None, str]]) -> None:
    """Persist URL changes to config.toml. No-op for 'keep', 'skip', 'clear-env-noop'."""
    from atlassian_skills.core.config import Profile, load_config, save_config

    config = load_config()
    if "default" not in config.profiles:
        config.profiles["default"] = Profile()
    prof = config.profiles["default"]
    changed = False
    for product, (url, action) in url_actions.items():
        field = f"{product}_url"
        if action == "set":
            setattr(prof, field, url)
            changed = True
        elif action == "clear-config":
            setattr(prof, field, None)
            changed = True
        # 'keep', 'skip', 'clear-env-noop' → no-op
    if changed:
        save_config(config)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def _is_tty() -> bool:
    """Indirection point so tests can monkeypatch the TTY check without reaching
    into typer.testing.CliRunner's substituted stdin (which would no-op the patch).
    """
    return sys.stdin.isatty()


def _ensure_interactive_terminal() -> None:
    """Refuse to prompt for secrets when stdin isn't a TTY.

    Guards against AI agents (Claude Code / Codex) running this through a non-TTY
    Bash tool — the wizard would hang on the prompt and the agent would try to
    fulfill the prompt by asking the user for a token in chat, leaking it into the
    LLM context. Tests bypass this via a `bypass_tty_guard` fixture that monkey-patches
    this function to a no-op.
    """
    if not _is_tty():
        typer.echo(
            "error: atls setup must be run in an interactive terminal.\n"
            "  Detected non-TTY stdin — refusing to prompt for secrets in this context.\n"
            "  If you're running through an AI agent's shell tool, run this in your own terminal instead.",
            err=True,
        )
        raise typer.Exit(1)


def _prompt_agent_install(label: str, default: bool = True) -> bool:
    """[y/n] prompt — uses the same `options [default=X]:` layout as the per-product
    prompts (`[k]eep / [e]dit / [r]emove / [s]kip [default=k]:`) so the marker position
    is consistent across the wizard."""
    marker = "Y" if default else "n"
    # `prompt_suffix=":"` is typer's default; we render the choices + default ourselves
    # because `typer.confirm` would otherwise inject "[Y/n]" itself, duplicating the marker.
    answer = (
        typer.prompt(
            f"  {label}? [y/n] [default={marker}]",
            default=marker,
            show_default=False,
        )
        .strip()
        .lower()
    )
    return answer in ("y", "yes")


# ---------------------------------------------------------------------------
# Skill refresh — used by wizard AND `setup --skills-only` (upgrade path)
# ---------------------------------------------------------------------------


def _refresh_skills(*, claude: bool, codex: bool, copilot: bool = False) -> list[str]:
    """Install canonical SKILL.md tree + inject routing blocks. Returns status messages."""
    msgs: list[str] = []
    if codex:
        msgs.extend(_install_tree(_CANONICAL_SKILL_DIR, _get_codex_skill_target().parent))
        msgs.append(_inject_codex_agents_block())
        legacy = _legacy_codex_skill_notice()
        if legacy:
            msgs.append(legacy)
    if claude:
        msgs.extend(_install_tree(_CANONICAL_SKILL_DIR, _get_claude_skill_target().parent))
        msgs.append(_inject_claude_md_block())
        legacy = _legacy_claude_command_notice()
        if legacy:
            msgs.append(legacy)
    if copilot:
        msgs.extend(_install_tree(_CANONICAL_SKILL_DIR, _get_copilot_skill_target().parent))
        msgs.append(_inject_copilot_instructions_block())
    return msgs


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------


def _print_fish_abort() -> None:
    typer.echo(
        "✗ fish shell detected. atls setup writes bash/zsh-compatible rc lines and\n"
        "  does not yet support fish's `set -gx` syntax. Workarounds:\n"
        "    1. Set the three env vars manually in ~/.config/fish/config.fish, or\n"
        "    2. Re-run `atls setup` from a bash/zsh session.\n"
        "    3. For skill-only install: `atls setup --skills-only`",
        err=True,
    )


def _wizard_product_step(  # noqa: C901 — sequential prompt narrative reads better inline
    product: str,
    index: int,
    total: int,
    url_state: dict[str, tuple[str | None, str | None]],
    token_state: dict[str, int],
    file_state: dict[str, bool],
) -> tuple[tuple[str | None, str], str | None]:
    """Walk one product (Jira/Confluence/Bitbucket) through its URL+PAT prompts.

    Returns ((url, url_action), new_token_or_None). `url_action` ∈ {'set', 'keep', 'skip',
    'clear-config', 'clear-env-noop'}. `new_token` is a freshly-entered raw token, or None
    if the user kept the existing one / had none to begin with.
    """
    label = product.capitalize()
    current_url, source = url_state[product]
    token_len = token_state.get(product, 0)
    has_token = token_len > 0
    has_file = file_state.get(product, False)

    typer.echo(f"[{index}/{total}] {label}")
    if current_url:
        typer.echo(f"  URL: {current_url}  ({source})")
    else:
        typer.echo("  URL: not configured")
    if has_token:
        typer.echo(f"  PAT: set (length={token_len})")
    elif has_file:
        typer.echo(f"  PAT: file ~/.secrets/{product}_pat present (env var not loaded)")
    else:
        typer.echo("  PAT: not set")

    # Show the [k/e/r/s] menu whenever ANY state exists (URL, env token, OR orphan file).
    # This is what makes the orphan-banner's `[r]emove below` promise actually reachable
    # when the user only has a stale file and no URL/env var.
    if current_url or has_token or has_file:
        options = "[k]eep / [e]dit / [r]emove / [s]kip"
        default = "k"
    else:
        options = "[a]dd / [s]kip"
        default = "s"
    choice = typer.prompt(f"  {options} [default={default}]", default=default, show_default=False).strip().lower()

    if choice in ("k", "keep") or (choice in ("s", "skip") and (current_url or has_token)):
        typer.echo("")
        return (current_url, "keep") if current_url else (None, "skip"), None
    if choice in ("s", "skip") and not (current_url or has_token):
        typer.echo("")
        return (None, "skip"), None
    if choice in ("r", "remove"):
        # Token file gets deleted regardless of URL source — the orphan banner promised
        # this, and keeping a stale ~/.secrets/{p}_pat after the user explicitly chose
        # `r` would be surprising.
        token_path = _secrets_dir() / f"{product}_pat"
        token_removed = False
        if token_path.exists():
            try:
                token_path.unlink()
                token_removed = True
            except OSError as e:
                typer.echo(f"  ⚠ failed to delete {token_path}: {e}", err=True)

        if source == "config":
            typer.echo("  → URL removed from config.toml")
            if token_removed:
                typer.echo(f"    Deleted token file ~/.secrets/{product}_pat")
            typer.echo("")
            return (None, "clear-config"), None
        if source and source.startswith("env"):
            if token_removed:
                typer.echo(f"  → Deleted token file ~/.secrets/{product}_pat")
            typer.echo(
                f"  ⚠ {label} URL is set via {source}; the wizard cannot permanently "
                "unset shell env vars. Remove that line from your shell rc manually.",
                err=True,
            )
            typer.echo("")
            return (current_url, "clear-env-noop"), None
        # No URL at all — pure orphan token file cleanup.
        if token_removed:
            typer.echo(f"  → Deleted token file ~/.secrets/{product}_pat")
        else:
            typer.echo("  → nothing to remove (no current value, no token file)")
        typer.echo("")
        return (None, "skip"), None
    if choice in ("e", "edit", "a", "add"):
        # URL prompt — Enter keeps current value when one exists; blank skips when not
        if current_url:
            new_url_raw = typer.prompt(
                f"  {label} URL (Enter to keep) [{current_url}]",
                default=current_url,
                show_default=False,
            ).strip()
        else:
            example = {
                "jira": "https://jira.example.com/",
                "confluence": "https://confluence.example.com/",
                "bitbucket": "https://bitbucket.example.com/",
            }[product]
            new_url_raw = typer.prompt(
                f"  {label} URL (blank to skip) (e.g. {example})", default="", show_default=False
            ).strip()
        if not new_url_raw:
            typer.echo("  → skipped (no URL entered)")
            typer.echo("")
            return (None, "skip"), None

        # PAT issuer hint — written instruction, no guessed URL (varies by deployment)
        typer.echo(f"  Generate a PAT: {_pat_issuer_hint(product)}")

        if has_token:
            pat_prompt = f"  {label} PAT [set, Enter to keep]"
        else:
            pat_prompt = f"  {label} PAT (hidden — typed characters won't appear, blank to skip)"
        new_token = typer.prompt(pat_prompt, default="", hide_input=True, show_default=False).strip()
        typer.echo("")
        return (new_url_raw, "set"), (new_token or None)

    typer.echo(f"  Unknown choice '{choice}', treating as keep/skip.")
    typer.echo("")
    return (current_url, "keep") if current_url else (None, "skip"), None


def _wizard() -> None:  # noqa: C901 — sequential narrative reads better than splitting
    _ensure_interactive_terminal()

    typer.echo(_AGENT_WARNING)
    typer.echo("")

    if _is_fish():
        _print_fish_abort()
        raise typer.Exit(0)

    platform_name = _detect_platform()
    shell = _detect_shell()
    typer.echo(f"Detected platform: {platform_name} (shell: {shell})")
    typer.echo("")

    # File/env mismatch banner — Unix only (Windows env is registry-backed, no source step).
    if platform_name != "windows":
        orphans = _orphan_token_files()
        if orphans:
            rc = _shell_rc_path()
            try:
                rc_disp = "~/" + str(rc.relative_to(Path.home()))
            except ValueError:
                rc_disp = str(rc)
            files_csv = " ".join(f"~/.secrets/{p}_pat" for p in orphans)
            typer.echo(
                "ℹ Token file(s) exist on disk but the env var(s) aren't loaded in this shell:\n"
                f"    {files_csv}\n"
                f"  To use them now:        source {rc_disp}\n"
                "  To overwrite them:      re-enter a PAT below (pick [e]dit)\n"
                f"  To delete the file(s):  pick [r]emove below, or run: rm {files_csv}\n"
            )

    url_state = _existing_url_state()
    token_state = _existing_tokens()
    file_state = _existing_file_state()

    url_actions: dict[str, tuple[str | None, str]] = {}
    new_tokens: dict[str, str] = {}
    total_steps = len(_PRODUCTS) + 1  # +1 for AI agent step

    for idx, product in enumerate(_PRODUCTS, start=1):
        (url, action), token = _wizard_product_step(product, idx, total_steps, url_state, token_state, file_state)
        url_actions[product] = (url, action)
        if token is not None:
            new_tokens[product] = token

    _apply_url_changes(url_actions)

    # Save tokens — Unix rebuilds the rc block from ALL existing ~/.secrets files so a
    # product that wasn't touched in this run keeps its export line (Bug 1 fix).
    if new_tokens:
        if platform_name == "windows":
            env_vars = {_TOKEN_ENV_NAMES[p]: t for p, t in new_tokens.items()}
            _save_tokens_windows(env_vars)
            typer.echo("  → tokens saved to Windows user env (HKCU\\Environment)")
        else:
            for product, token in new_tokens.items():
                _save_token_unix(product, token)
            msg = _inject_shell_env_block_from_disk()
            typer.echo(f"  {msg.strip()}")
        typer.echo("")

    # B1 warning — manual exports outside the atls block can shadow wizard-managed tokens
    if platform_name != "windows":
        for warning in _detect_rc_shadowing():
            typer.echo(f"⚠ {warning}", err=True)

    # ATLS_DEFAULT_*_TOKEN shadowing: the advanced-multi-profile env var takes priority
    # over the legacy `JIRA_PERSONAL_TOKEN` etc., so saving a new token via the wizard
    # has no effect until the ATLS_* form is unset.
    if new_tokens:
        for warning in _detect_atls_default_shadowing(list(new_tokens.keys())):
            typer.echo(f"⚠ {warning}", err=True)

    # AI agent step — defaults to Yes for all three agents. `atls upgrade` (--skills-only)
    # still respects opt-in: it only refreshes Copilot when SKILL.md already exists, so
    # existing Claude+Codex users aren't surprise-installed during a routine upgrade.
    typer.echo(f"[{len(_PRODUCTS) + 1}/{total_steps}] AI agent skills")
    install_claude = _prompt_agent_install("Install Claude Code skill", default=True)
    install_codex = _prompt_agent_install("Install Codex skill", default=True)
    install_copilot = _prompt_agent_install("Install GitHub Copilot skill", default=True)
    if install_claude or install_codex or install_copilot:
        for msg in _refresh_skills(claude=install_claude, codex=install_codex, copilot=install_copilot):
            typer.echo(f"  {msg.strip()}")
    if install_copilot and _is_wsl():
        typer.echo(
            "  ⚠ WSL detected: ~/.copilot lives in your WSL filesystem and is invisible to a\n"
            "    native Windows Copilot CLI install. If you use Copilot CLI on Windows directly,\n"
            "    re-run `atls setup` from a Windows shell (cmd / PowerShell / Git Bash)."
        )
    typer.echo("")

    # Final guidance — shell-agnostic. "Open a new terminal" is the universal answer:
    # it works regardless of zsh/bash/cmd/PowerShell/Git Bash and skips the rc-file
    # detection guessing game entirely.
    typer.echo("Next steps:")
    typer.echo("  • Open a new terminal to use the new env vars (works on every shell + platform).")
    if platform_name == "windows":
        typer.echo("    No reboot needed — HKCU\\Environment is read by every new process.")
    typer.echo("  • Already-running apps that read env at startup (IDEs, editors) need a restart.")
    if install_claude or install_codex:
        typer.echo(
            "  • AI agent skills: Claude Code / Codex auto-load `atls` on the next session. "
            "For an already-open session, start a new chat or reload skills."
        )
    typer.echo("")

    # Verify — os.environ has been updated in-process by _save_token_unix / _save_tokens_windows
    typer.echo("Verifying...")
    typer.echo("")
    from atlassian_skills.cli.auth import render_auth_status

    render_auth_status("default")


# ---------------------------------------------------------------------------
# Setup entry callback + --skills-only
# ---------------------------------------------------------------------------


@setup_app.callback(invoke_without_command=True)
def setup_entry(
    ctx: typer.Context,
    skills_only: bool = typer.Option(
        False,
        "--skills-only",
        help="Silently reinstall Claude/Codex skill assets (used by `atls upgrade`).",
    ),
) -> None:
    """Run the interactive setup wizard. With --skills-only, refresh skill assets silently."""
    if ctx.invoked_subcommand is not None:
        return
    if skills_only:
        for msg in _refresh_skills(claude=True, codex=True, copilot=_get_copilot_skill_target().exists()):
            typer.echo(msg)
        return
    # Reject non-default --profile: the wizard's URL/token storage paths are all keyed
    # to "default" (config.toml profile name, JIRA_PERSONAL_TOKEN env var, etc.).
    # Multi-profile setup is planned for 0.4.0; for now, edit config.toml directly.
    ctx.ensure_object(dict)
    profile_name = ctx.obj.get("profile", "default")
    if profile_name != "default":
        typer.echo(
            f"error: `atls setup` currently supports only the 'default' profile "
            f"(got --profile={profile_name!r}).\n"
            "  Multi-profile setup is planned for 0.4.0. For now, edit\n"
            "  ~/.config/atlassian-skills/config.toml manually for non-default profiles.",
            err=True,
        )
        raise typer.Exit(1)
    _wizard()


# ---------------------------------------------------------------------------
# Legacy subcommands — DEPRECATED, removed in 0.3.0
#
# These reproduce 0.2.6 behaviour to keep existing automation working through the
# 0.2.7 release. Each one emits a stderr deprecation line and otherwise behaves
# identically to 0.2.6. `atls upgrade` itself does NOT call any of these — it calls
# `atls setup --skills-only` directly so users don't see warnings on every upgrade.
# ---------------------------------------------------------------------------


def _show_paths() -> None:
    typer.echo(f"Platform: {_detect_platform()}")
    typer.echo(f"  Claude config dir         : {_get_claude_config_dir()}")
    typer.echo(f"  Claude skill target       : {_get_claude_skill_target()}")
    typer.echo(f"  Claude command (legacy)   : {_get_claude_command_target()}")
    typer.echo(f"  CLAUDE.md path            : {_get_claude_md_path()}")
    typer.echo(f"  Codex config dir          : {_get_codex_config_dir()}")
    typer.echo(f"  Codex AGENTS.md path      : {_get_codex_agents_path()}")
    typer.echo(f"  Codex skill target        : {_get_codex_skill_target()}  (canonical)")
    typer.echo(f"  Codex legacy skill target : {_get_codex_legacy_target()}  (detection only)")


@setup_app.command("codex")
def setup_codex() -> None:
    """[deprecated] Install atls skill for Codex. Use `atls setup` (wizard) instead."""
    _emit_deprecation("codex")
    for msg in _refresh_skills(claude=False, codex=True):
        typer.echo(msg)


@setup_app.command("claude")
def setup_claude() -> None:
    """[deprecated] Install atls skill for Claude Code. Use `atls setup` (wizard) instead."""
    _emit_deprecation("claude")
    for msg in _refresh_skills(claude=True, codex=False):
        typer.echo(msg)


@setup_app.command("all")
def setup_all() -> None:
    """[deprecated] Install skills for both Codex and Claude Code. Use `atls setup` instead."""
    _emit_deprecation("all")
    for msg in _refresh_skills(claude=True, codex=True):
        typer.echo(msg)


@setup_app.command("paths")
def setup_paths() -> None:
    """[deprecated] Show all resolved install paths. Use `atls doctor` instead."""
    _emit_deprecation("paths")
    _show_paths()


@setup_app.command("status")
def setup_status() -> None:
    """[deprecated] Check installation status. Use `atls doctor` instead."""
    _emit_deprecation("status")
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
