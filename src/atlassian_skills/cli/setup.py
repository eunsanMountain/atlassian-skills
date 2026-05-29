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
    """Detect fish shell (used by `atls doctor` for shell display)."""
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
# Token storage — keyring (0.2.8)
# ---------------------------------------------------------------------------


def _detect_headless() -> list[str]:
    """Return advisory signals that an OS keyring backend may be locked / unavailable.

    None of these are definitive — they are hints the wizard surfaces before the user
    commits to keyring storage in an environment where it frequently cannot unlock
    (Docker, WSL, headless server, text-only SSH). The user can still proceed.
    """
    signals: list[str] = []
    if Path("/.dockerenv").exists():
        signals.append("running inside Docker (/.dockerenv present)")
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        signals.append("WSL2 — the native Windows credential store is separate from this session")
    if sys.platform.startswith("linux") and not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        signals.append("no D-Bus session bus — likely no desktop session, so GNOME Keyring / KWallet stays locked")
    if os.environ.get("SSH_CONNECTION") and not os.environ.get("DISPLAY"):
        signals.append("SSH session without DISPLAY (text-only)")
    return signals


def _detect_installer() -> str:
    """Best-effort guess at how atls was installed, to print the matching keyring-extra command."""
    # Check BOTH the raw and the symlink-resolved path: a uv tool venv's python is a symlink
    # into the shared `…/uv/python/<cpython>/bin` dir, so resolve() alone drops the `/uv/tools/`
    # marker and would misreport uv installs as pip.
    raw = str(Path(sys.executable)).replace("\\", "/")
    resolved = str(Path(sys.executable).resolve()).replace("\\", "/")
    haystack = f"{raw}\n{resolved}"
    if "/uv/tools/" in haystack or "/uv/tool/" in haystack:
        return "uv"
    if "pipx" in haystack:
        return "pipx"
    return "pip"


def _keyring_install_hint() -> str:
    """The exact install command for the optional keyring extra, matched to the installer."""
    return {
        "uv": 'uv tool install --force "atlassian-skills[keyring]"',
        "pipx": "pipx inject atlassian-skills keyring",
        "pip": 'pip install "atlassian-skills[keyring]"',
    }[_detect_installer()]


def _save_token_keyring(profile_name: str, product: str, token: str) -> None:
    """Store a token in the OS keyring under the exact key the resolver reads.

    Service `atls-<profile>`, account `<product>_token` — must stay in lockstep with
    `core.auth._resolve_token_from_provider`, which calls
    `keyring.get_password(f"atls-{profile_name}", f"{product}_token")`.
    """
    import keyring  # noqa: PLC0415 — optional dep, imported lazily; ImportError handled by caller

    keyring.set_password(f"atls-{profile_name}", f"{product}_token", token)


def _delete_token_keyring(profile_name: str, product: str) -> bool:
    """Best-effort removal of a keyring entry (used by the `[r]emove` action).

    Returns True only when an entry was actually deleted. A missing entry, an absent
    backend, or a locked store all return False — the caller phrases its message off this
    so it never claims to have cleared something that wasn't there.
    """
    try:
        import keyring  # noqa: PLC0415

        keyring.delete_password(f"atls-{profile_name}", f"{product}_token")
        return True
    except Exception:
        return False


def _set_profile_storage(storage: str) -> None:
    """Persist the storage mode to [profiles.default]."""
    from atlassian_skills.core.config import Profile, load_config, save_config

    config = load_config()
    if "default" not in config.profiles:
        config.profiles["default"] = Profile()
    prof = config.profiles["default"]
    prof.storage = storage  # type: ignore[assignment]  # validated set: env|keyring|command
    save_config(config)


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


def _env_token_var(product: str, profile_name: str = "default") -> str | None:
    """Return the name of whichever token env var is actually set for `product`, or None.

    Prefers the new `ATLS_<PROFILE>_<PRODUCT>_TOKEN` form when set, else the legacy name
    from `_TOKEN_ENV_NAMES`. Mirrors `get_env_token`'s resolution order so the wizard names
    the exact variable the resolver reads.
    """
    atls_var = f"ATLS_{profile_name.upper()}_{product.upper()}_TOKEN"
    if os.environ.get(atls_var):
        return atls_var
    legacy = _TOKEN_ENV_NAMES.get(product)
    if legacy and os.environ.get(legacy):
        return legacy
    return None


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


def _wizard_product_step(  # noqa: C901 — sequential prompt narrative reads better inline
    product: str,
    index: int,
    total: int,
    url_state: dict[str, tuple[str | None, str | None]],
    env_products: list[str],
) -> tuple[tuple[str | None, str], str | None]:
    """Walk one product (Jira/Confluence/Bitbucket) through its URL + secret prompts.

    Returns ((url, url_action), new_secret_or_None). `url_action` ∈ {'set', 'keep', 'skip',
    'clear-config', 'clear-env-noop'}. The second element is the freshly-entered raw PAT to
    store in the OS keyring, or None when the user kept the existing one / skipped / the
    product's token already lives in the environment (env outranks keyring — see `env_products`).
    """
    label = product.capitalize()
    current_url, source = url_state[product]
    in_env = product in env_products

    typer.echo(f"[{index}/{total}] {label}")
    if current_url:
        typer.echo(f"  URL: {current_url}  ({source})")
    else:
        typer.echo("  URL: not configured")
    # PAT display. A live env token always shadows the keyring (env > keyring), so the wizard
    # never writes a keyring entry for an env product — it says so plainly instead. Otherwise the
    # token lives in the keyring (we don't probe it here — that would trigger a per-product unlock
    # prompt; the final `--resolve` verify probes once).
    if in_env:
        varname = _env_token_var(product)
        typer.echo(f"  PAT: environment variable ({varname}) — atls uses this; keyring skipped")
    else:
        typer.echo("  PAT: keyring — not checked here; [e]dit to set/replace (verify at the end shows what resolves)")

    # Menu verbs: `skip` (leave as-is / don't configure — the no-op), `edit` (add or change
    # URL + keyring token), `remove` (delete). `skip`/`edit`/`remove` are shown whenever ANY
    # state exists (URL configured or token in the environment).
    #
    # Default: `skip` when a URL is configured (Enter keeps it as-is); else `edit` to nudge the
    # user to set the URL. (Legacy `k`/`a` inputs are still accepted as aliases for skip/edit.)
    if current_url or in_env:
        options = "[s]kip / [e]dit / [r]emove"
        default = "s" if current_url else "e"
    else:
        options = "[s]kip / [e]dit"
        default = "s"
    choice = typer.prompt(f"  {options} [default={default}]", default=default, show_default=False).strip().lower()

    if choice in ("k", "keep") or (choice in ("s", "skip") and (current_url or in_env)):
        typer.echo("")
        return (current_url, "keep") if current_url else (None, "skip"), None
    if choice in ("s", "skip") and not (current_url or in_env):
        typer.echo("")
        return (None, "skip"), None
    if choice in ("r", "remove"):
        # Best-effort clear of the keyring entry. We never touch env vars or shell rc files —
        # those are the user's manual setup; they unset them themselves.
        removed = _delete_token_keyring("default", product)
        if removed:
            typer.echo(f"  → cleared {label} keyring entry")
        if in_env:
            # The live credential is an env var the wizard never manages — say so, otherwise the
            # user thinks `remove` killed it while atls keeps resolving the (still-set) env token.
            varname = _env_token_var(product)
            typer.echo(
                f"  ⚠ {label} token comes from {varname} (environment) and is still active — "
                "unset it + open a new terminal to fully remove."
            )
        elif not removed:
            typer.echo(f"  → no {label} keyring entry to remove")

        if source == "config":
            typer.echo("  → URL removed from config.toml")
            typer.echo("")
            return (None, "clear-config"), None
        if source and source.startswith("env"):
            typer.echo(
                f"  ⚠ {label} URL is set via {source}; the wizard cannot permanently "
                "unset shell env vars. Remove that line from your shell rc manually.",
                err=True,
            )
            typer.echo("")
            return (current_url, "clear-env-noop"), None
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

        # A live env token shadows any keyring entry, so don't prompt for / store one. Save the
        # URL but leave the token in the environment (the user moves it to the keyring by hand).
        if in_env:
            varname = _env_token_var(product)
            typer.echo(
                f"  → {label} token stays in the environment; unset {varname} + new terminal to use the keyring instead"
            )
            typer.echo("")
            return (new_url_raw, "set"), None

        # PAT issuer hint — written instruction, no guessed URL (varies by deployment)
        typer.echo(f"  Generate a PAT: {_pat_issuer_hint(product)}")
        new_token = typer.prompt(
            f"  {label} PAT (hidden — typed characters won't appear, blank to skip)",
            default="",
            hide_input=True,
            show_default=False,
        ).strip()
        typer.echo("")
        return (new_url_raw, "set"), (new_token or None)

    # Do NOT echo the raw input — a user may have pasted a token at this prompt by mistake,
    # and reflecting it would leak the secret into the terminal scrollback.
    typer.echo("  Unrecognized choice, treating as skip.")
    typer.echo("")
    return (current_url, "keep") if current_url else (None, "skip"), None


def _wizard() -> None:  # noqa: C901 — sequential narrative reads better than splitting
    _ensure_interactive_terminal()

    typer.echo(_AGENT_WARNING)
    typer.echo("")

    platform_name = _detect_platform()
    shell = _detect_shell()
    typer.echo(f"Detected platform: {platform_name} (shell: {shell})")
    typer.echo("")

    from atlassian_skills.core.config import get_env_token, get_profile, load_config

    # Command-storage notice. The wizard only manages keyring; it cannot see or write a
    # shell-command provider. Saving any PAT below flips the WHOLE profile to storage=keyring,
    # which would silently break command-based resolution for the other products. Warn up front
    # (non-silent) and point command users at the manual config path instead.
    current_storage = get_profile(load_config(), "default").storage
    if current_storage == "command":
        typer.echo(
            "⚠ This profile uses storage='command' (shell-command secret manager).\n"
            "  The wizard manages the OS keyring only — it cannot edit your command setup. If you\n"
            "  set a PAT below, the ENTIRE profile switches to storage='keyring', which will break\n"
            "  command-based resolution for every product. To keep using commands, edit\n"
            "  config.toml directly (README → Manual setup) and skip the PAT prompts here."
        )
        typer.echo("")

    # Env-token detection. The resolver does env > keyring, so a live env token always shadows a
    # keyring entry. Rather than write a shadowed (and therefore useless) keyring entry, the wizard
    # tells the user and SKIPS those products. We never delete their env/rc — they do that.
    env_products = [p for p in _PRODUCTS if get_env_token("default", p)]
    if env_products:
        typer.echo("ℹ atls is already reading token(s) from your environment:")
        for p in env_products:
            typer.echo(f"    {p.upper()}: {_env_token_var(p)}")
        typer.echo(
            "  Environment variables take priority over the keyring, so this wizard — which stores\n"
            "  tokens in the OS keyring only — will SKIP those products (a keyring entry would just be\n"
            "  shadowed). Your env setup keeps working untouched.\n"
            "\n"
            "  • To move a product to the keyring: unset its env var, remove it from your shell rc,\n"
            "    open a NEW terminal, then re-run `atls setup`.\n"
            "  • To keep using environment variables (or use a shell-command secret manager): see\n"
            "    README → Manual setup. The wizard won't manage those."
        )
        typer.echo("")

    url_state = _existing_url_state()

    url_actions: dict[str, tuple[str | None, str]] = {}
    new_secrets: dict[str, str] = {}  # raw PATs to store in the keyring, keyed by product
    total_steps = len(_PRODUCTS) + 1  # +1 for AI agent step

    for idx, product in enumerate(_PRODUCTS, start=1):
        (url, action), secret = _wizard_product_step(product, idx, total_steps, url_state, env_products)
        url_actions[product] = (url, action)
        if secret is not None:
            new_secrets[product] = secret

    _apply_url_changes(url_actions)

    if new_secrets:
        # Surface the headless caveat BEFORE writing — these are exactly the sessions where the
        # keyring write itself can fail, so the warning must not be gated behind a successful save.
        headless_signals = _detect_headless()
        if headless_signals:
            typer.echo(
                f"  ⚠ this session may not be able to unlock the OS keyring ({headless_signals[0]});\n"
                "    if the write or later reads fail, use environment variables instead "
                "(README → Manual setup).",
                err=True,
            )
        try:
            for product, token in new_secrets.items():
                _save_token_keyring("default", product, token)
        except ImportError:
            typer.echo(f"  ✗ keyring not available — install it: {_keyring_install_hint()}", err=True)
            raise typer.Exit(1) from None
        except Exception as exc:  # noqa: BLE001 — keyring backend failure (NoKeyringError / locked / init)
            # No usable/unlockable backend (headless Linux without D-Bus, locked Keychain, …).
            # keyring raises keyring.errors.KeyringError here, NOT ImportError — catch it so the
            # wizard exits cleanly instead of dumping a traceback at the user.
            typer.echo(f"  ✗ couldn't write to the OS keyring: {exc}", err=True)
            typer.echo(
                "    This session likely can't unlock a keyring backend. Use environment variables\n"
                "    instead (README → Manual setup), or run from an unlocked desktop session.",
                err=True,
            )
            raise typer.Exit(1) from None
        typer.echo(f"  → saved {len(new_secrets)} token(s) to the OS keyring (service 'atls-default')")
        if platform_name == "macos":
            typer.echo('    macOS: click "Always Allow" on first access so atls isn\'t re-prompted each call.')
        # Persist storage=keyring now that a keyring token exists. A pure env user who ran the
        # wizard for URLs/skills only (empty new_secrets) must NOT get storage flipped.
        _set_profile_storage("keyring")
        typer.echo("")

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

    # Final guidance — keyring-focused; tokens are read on each call, so no shell restart is needed.
    typer.echo("Next steps:")
    typer.echo("  • Tokens live in the OS keyring — no shell restart needed.")
    if env_products:
        typer.echo("  • Your environment-based tokens are unchanged and still take priority.")
    if install_claude or install_codex:
        typer.echo(
            "  • AI agent skills: Claude Code / Codex auto-load `atls` on the next session. "
            "For an already-open session, start a new chat or reload skills."
        )
    typer.echo("")

    # Verify — probe the configured provider so the user sees the credential actually resolves.
    # resolve=True exercises the freshly-saved keyring entry (any unlock prompt is expected here —
    # this is the verification step).
    typer.echo("Verifying...")
    typer.echo("")
    from atlassian_skills.cli.auth import render_auth_status

    render_auth_status("default", resolve=True)


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
