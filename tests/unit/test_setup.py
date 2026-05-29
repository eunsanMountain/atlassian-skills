"""Tests for cli/setup.py — keyring-only wizard, shim, helpers, TTY guard, no-token-echo."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from atlassian_skills.cli.setup import (
    _claude_md_block,
    _inject_claude_md_block,
    _inject_codex_agents_block,
    _inject_copilot_instructions_block,
    _install,
)

# ---------------------------------------------------------------------------
# Preserved helper tests — _install, marker block injection
# ---------------------------------------------------------------------------


class TestInstall:
    def test_install_creates_file(self, tmp_path: Path) -> None:
        source = tmp_path / "source.md"
        source.write_text("# Hello", encoding="utf-8")
        target = tmp_path / "sub" / "dir" / "target.md"

        msg = _install(source, target)

        assert target.exists()
        assert target.read_text(encoding="utf-8") == "# Hello"
        assert "installed" in msg
        assert str(target) in msg

    def test_install_backup_existing(self, tmp_path: Path) -> None:
        source = tmp_path / "source.md"
        source.write_text("# New content", encoding="utf-8")
        target = tmp_path / "target.md"
        target.write_text("# Old content", encoding="utf-8")

        msg = _install(source, target)

        assert target.read_text(encoding="utf-8") == "# New content"
        backup = target.with_suffix(target.suffix + ".bak")
        assert backup.exists()
        assert backup.read_text(encoding="utf-8") == "# Old content"
        assert "updated" in msg
        assert "backup" in msg

    def test_install_identical_content_short_circuits(self, tmp_path: Path) -> None:
        """Bytes-identical source+target should NOT churn a backup — saves `atls upgrade` from
        accumulating .bak files on every no-op patch release."""
        content = "# Same content"
        source = tmp_path / "source.md"
        source.write_text(content, encoding="utf-8")
        target = tmp_path / "target.md"
        target.write_text(content, encoding="utf-8")

        msg = _install(source, target)

        assert "unchanged" in msg
        # No .bak created for identical content
        backup = target.with_suffix(target.suffix + ".bak")
        assert not backup.exists()


class TestInjectClaudeMdBlock:
    def test_creates_claude_md_if_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.setup as setup_mod

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(setup_mod, "_get_claude_md_path", lambda: tmp_path / ".claude" / "CLAUDE.md")

        msg = _inject_claude_md_block()

        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        assert claude_md.exists()
        content = claude_md.read_text(encoding="utf-8")
        assert "ATLS-CLAUDE:START" in content
        assert "ATLS-CLAUDE:END" in content
        assert "atls" in content.lower()
        assert "created" in msg

    def test_appends_block_to_existing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.setup as setup_mod

        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        claude_md.parent.mkdir(parents=True)
        claude_md.write_text("# My CLAUDE.md\n\nExisting content.\n", encoding="utf-8")
        monkeypatch.setattr(setup_mod, "_get_claude_md_path", lambda: claude_md)

        msg = _inject_claude_md_block()

        content = claude_md.read_text(encoding="utf-8")
        assert content.startswith("# My CLAUDE.md")
        assert "ATLS-CLAUDE:START" in content
        assert "appended" in msg

    def test_replaces_existing_block(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.setup as setup_mod

        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        claude_md.parent.mkdir(parents=True)
        old_content = (
            "# My CLAUDE.md\n\n"
            "<!-- ATLS-CLAUDE:START -->\n<!-- ATLS:VERSION:0.0.1 -->\nOld block\n<!-- ATLS-CLAUDE:END -->\n\n"
            "# User stuff\n"
        )
        claude_md.write_text(old_content, encoding="utf-8")
        monkeypatch.setattr(setup_mod, "_get_claude_md_path", lambda: claude_md)

        msg = _inject_claude_md_block()

        content = claude_md.read_text(encoding="utf-8")
        assert "0.0.1" not in content
        assert "ATLS:VERSION:" in content
        assert "# User stuff" in content
        assert "updated" in msg

    def test_updates_when_identical(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.setup as setup_mod

        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        claude_md.parent.mkdir(parents=True)
        claude_md.write_text(_claude_md_block() + "\n", encoding="utf-8")
        monkeypatch.setattr(setup_mod, "_get_claude_md_path", lambda: claude_md)

        msg = _inject_claude_md_block()

        assert "updated" in msg


class TestInjectCodexAgentsBlock:
    def test_creates_codex_agents_if_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.setup as setup_mod

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(setup_mod, "_get_codex_agents_path", lambda: tmp_path / ".codex" / "AGENTS.md")

        msg = _inject_codex_agents_block()

        agents_md = tmp_path / ".codex" / "AGENTS.md"
        assert agents_md.exists()
        content = agents_md.read_text(encoding="utf-8")
        assert "ATLS-CODEX:START" in content
        assert "ATLS-CODEX:END" in content
        assert "$atls" in content
        assert "created" in msg

    def test_appends_codex_block_to_existing_agents(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.setup as setup_mod

        agents_md = tmp_path / ".codex" / "AGENTS.md"
        agents_md.parent.mkdir(parents=True)
        agents_md.write_text("# Global rules\n\nExisting content.\n", encoding="utf-8")
        monkeypatch.setattr(setup_mod, "_get_codex_agents_path", lambda: agents_md)

        msg = _inject_codex_agents_block()

        content = agents_md.read_text(encoding="utf-8")
        assert content.startswith("# Global rules")
        assert "ATLS-CODEX:START" in content
        assert "appended" in msg


class TestInjectCopilotInstructionsBlock:
    """Mirror of TestInjectCodexAgentsBlock — Copilot's ~/.copilot/copilot-instructions.md
    is the routing-block equivalent of AGENTS.md / CLAUDE.md."""

    def test_creates_copilot_instructions_if_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.setup as setup_mod

        instructions = tmp_path / ".copilot" / "copilot-instructions.md"
        monkeypatch.setattr(setup_mod, "_get_copilot_instructions_path", lambda: instructions)

        msg = _inject_copilot_instructions_block()

        assert instructions.exists()
        content = instructions.read_text(encoding="utf-8")
        assert "ATLS-COPILOT:START" in content
        assert "ATLS-COPILOT:END" in content
        # Copilot CLI reads this as plain global guidance — must point at SKILL.md
        assert "skills/atls/SKILL.md" in content
        assert "created" in msg

    def test_appends_copilot_block_to_existing_instructions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import atlassian_skills.cli.setup as setup_mod

        instructions = tmp_path / ".copilot" / "copilot-instructions.md"
        instructions.parent.mkdir(parents=True)
        instructions.write_text("# Personal Copilot rules\n\nExisting content.\n", encoding="utf-8")
        monkeypatch.setattr(setup_mod, "_get_copilot_instructions_path", lambda: instructions)

        msg = _inject_copilot_instructions_block()

        content = instructions.read_text(encoding="utf-8")
        assert content.startswith("# Personal Copilot rules")
        assert "ATLS-COPILOT:START" in content
        assert "appended" in msg

    def test_replaces_existing_copilot_block_on_reinstall(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-running `atls setup` must update the ATLS block in-place, not duplicate it."""
        import atlassian_skills.cli.setup as setup_mod

        instructions = tmp_path / ".copilot" / "copilot-instructions.md"
        monkeypatch.setattr(setup_mod, "_get_copilot_instructions_path", lambda: instructions)

        _inject_copilot_instructions_block()
        first = instructions.read_text(encoding="utf-8")
        _inject_copilot_instructions_block()
        second = instructions.read_text(encoding="utf-8")

        # Exactly one START marker (no duplication)
        assert second.count("ATLS-COPILOT:START") == 1
        assert first == second  # Idempotent — same version, same content


# ---------------------------------------------------------------------------
# Shim subcommands — deprecation warning + behaviour preserved (0.2.7 → 0.3.0)
# ---------------------------------------------------------------------------


def _stub_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, asset_root: Path) -> None:
    """Common test stub — redirect all install paths under tmp_path."""
    import atlassian_skills.cli.setup as setup_mod

    monkeypatch.setattr(setup_mod, "ASSETS_DIR", asset_root)
    monkeypatch.setattr(setup_mod, "_CANONICAL_SKILL_DIR", asset_root / "skills" / "atls")
    monkeypatch.setattr(
        setup_mod, "_get_codex_skill_target", lambda: tmp_path / ".codex" / "skills" / "atls" / "SKILL.md"
    )
    monkeypatch.setattr(
        setup_mod, "_get_codex_legacy_target", lambda: tmp_path / ".agents" / "skills" / "atls" / "SKILL.md"
    )
    monkeypatch.setattr(setup_mod, "_get_codex_agents_path", lambda: tmp_path / ".codex" / "AGENTS.md")
    monkeypatch.setattr(
        setup_mod, "_get_claude_skill_target", lambda: tmp_path / ".claude" / "skills" / "atls" / "SKILL.md"
    )
    monkeypatch.setattr(setup_mod, "_get_claude_command_target", lambda: tmp_path / ".claude" / "commands" / "atls.md")
    monkeypatch.setattr(setup_mod, "_get_claude_md_path", lambda: tmp_path / ".claude" / "CLAUDE.md")
    monkeypatch.setattr(
        setup_mod, "_get_copilot_skill_target", lambda: tmp_path / ".copilot" / "skills" / "atls" / "SKILL.md"
    )
    monkeypatch.setattr(
        setup_mod, "_get_copilot_instructions_path", lambda: tmp_path / ".copilot" / "copilot-instructions.md"
    )


def _make_asset_root(tmp_path: Path) -> Path:
    asset_root = tmp_path / "assets"
    skill_dir = asset_root / "skills" / "atls"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("<!-- installed-by: atls 0.2.7 -->", encoding="utf-8")
    return asset_root


class TestSetupCodexShim:
    def test_codex_emits_deprecation_and_installs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.setup as setup_mod

        asset_root = _make_asset_root(tmp_path)
        _stub_paths(monkeypatch, tmp_path, asset_root)

        runner = CliRunner()
        result = runner.invoke(setup_mod.setup_app, ["codex"])

        assert result.exit_code == 0
        assert (tmp_path / ".codex" / "skills" / "atls" / "SKILL.md").exists()
        agents_content = (tmp_path / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
        assert "ATLS-CODEX:START" in agents_content
        # Deprecation warning on stderr only
        assert "deprecated" in result.output
        assert "0.3.0" in result.output


class TestSetupAllShim:
    def test_all_emits_deprecation_and_installs_both(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.setup as setup_mod

        asset_root = _make_asset_root(tmp_path)
        _stub_paths(monkeypatch, tmp_path, asset_root)

        runner = CliRunner()
        result = runner.invoke(setup_mod.setup_app, ["all"])

        assert result.exit_code == 0
        assert (tmp_path / ".codex" / "skills" / "atls" / "SKILL.md").exists()
        assert (tmp_path / ".claude" / "skills" / "atls" / "SKILL.md").exists()
        assert "deprecated" in result.output


class TestSetupStatusShim:
    def test_status_not_installed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        from atlassian_skills.cli.setup import setup_app

        runner = CliRunner()
        result = runner.invoke(setup_app, ["status"])

        assert result.exit_code == 0
        assert "not installed" in result.output
        assert "deprecated" in result.output

    def test_status_installed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        codex_target = tmp_path / ".codex" / "skills" / "atls" / "SKILL.md"
        codex_target.parent.mkdir(parents=True)
        codex_target.write_text("<!-- installed-by: atls 0.2.7 -->", encoding="utf-8")

        from atlassian_skills.cli.setup import setup_app

        runner = CliRunner()
        result = runner.invoke(setup_app, ["status"])

        assert result.exit_code == 0
        assert "v0.2.7" in result.output
        assert "Codex skill" in result.output
        assert "deprecated" in result.output


class TestSetupPathsShim:
    def test_paths_emits_deprecation_and_prints_paths(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        from atlassian_skills.cli.setup import setup_app

        runner = CliRunner()
        result = runner.invoke(setup_app, ["paths"])

        assert result.exit_code == 0
        assert "Platform:" in result.output
        assert "deprecated" in result.output


# ---------------------------------------------------------------------------
# `setup --skills-only` — silent non-interactive skill refresh (upgrade path)
# ---------------------------------------------------------------------------


class TestSkillsOnly:
    def test_skills_only_installs_silently(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.setup as setup_mod

        asset_root = _make_asset_root(tmp_path)
        _stub_paths(monkeypatch, tmp_path, asset_root)

        runner = CliRunner()
        result = runner.invoke(setup_mod.setup_app, ["--skills-only"])

        assert result.exit_code == 0
        assert (tmp_path / ".claude" / "skills" / "atls" / "SKILL.md").exists()
        assert (tmp_path / ".codex" / "skills" / "atls" / "SKILL.md").exists()
        # No deprecation warning — this is the canonical upgrade path
        assert "deprecated" not in result.output
        assert "deprecated" not in result.output


# ---------------------------------------------------------------------------
# Wizard — interactive flow
# ---------------------------------------------------------------------------


@pytest.fixture
def wizard_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bypass_tty_guard: None) -> Path:
    """Common wizard sandbox: tmp HOME, stub all install paths, neutralise Windows path.

    The wizard is keyring-only, so this fixture injects a MagicMock `keyring` module (its
    `get_password` returns a real string so the final `--resolve` verify works) and stubs
    `_detect_headless`→[] so no token test ever touches the real OS keyring. Tests that need a
    specific keyring behaviour can still install their own `patch.dict('sys.modules', ...)`.
    """
    import sys as _sys
    from unittest.mock import MagicMock as _MagicMock

    import atlassian_skills.cli.setup as setup_mod
    import atlassian_skills.core.config as config_mod

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    # config.toml is resolved via platformdirs — redirect to tmp_path
    monkeypatch.setattr(config_mod, "config_path", lambda: tmp_path / "config.toml")
    # Asset paths
    asset_root = _make_asset_root(tmp_path)
    _stub_paths(monkeypatch, tmp_path, asset_root)
    # Always pretend we're on Linux for these tests (fish env neutralised)
    monkeypatch.setattr(setup_mod, "_detect_platform", lambda: "linux")
    monkeypatch.setattr(setup_mod, "_detect_headless", lambda: [])
    monkeypatch.delenv("FISH_VERSION", raising=False)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    # Default keyring stub so URL+PAT tests don't reach the real OS keyring.
    mock_keyring = _MagicMock()
    mock_keyring.get_password.return_value = "resolved-token"
    monkeypatch.setitem(_sys.modules, "keyring", mock_keyring)
    # Clear any leftover token env vars from the host
    for name in ("JIRA_PERSONAL_TOKEN", "CONFLUENCE_PERSONAL_TOKEN", "BITBUCKET_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    for var in (
        "ATLS_DEFAULT_JIRA_URL",
        "ATLS_DEFAULT_CONFLUENCE_URL",
        "ATLS_DEFAULT_BITBUCKET_URL",
        "ATLS_DEFAULT_JIRA_TOKEN",
        "ATLS_DEFAULT_CONFLUENCE_TOKEN",
        "ATLS_DEFAULT_BITBUCKET_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _wizard_input(
    jira: tuple[str, ...] = ("s",),
    conf: tuple[str, ...] = ("s",),
    bb: tuple[str, ...] = ("s",),
    install_claude: str = "n",
    install_codex: str = "n",
    install_copilot: str = "n",
) -> str:
    """Build wizard stdin.

    The wizard is keyring-only — there is no storage prompt. Each product tuple is (action,) or
    (action, url, pat). The product-block walks Jira → Confluence → Bitbucket, then the agent step
    asks claude/codex/copilot.
    """
    lines: list[str] = []
    for spec in (jira, conf, bb):
        lines.extend(spec)
    lines += [install_claude, install_codex, install_copilot]
    return "\n".join(lines) + "\n"


class TestWizardURLs:
    def test_add_jira_only(self, wizard_env: Path) -> None:
        from atlassian_skills.cli.main import app
        from atlassian_skills.core.config import load_config

        runner = CliRunner()
        result = runner.invoke(
            app,
            ["setup"],
            input=_wizard_input(jira=("a", "https://jira.example.com", "t-jira")),
        )

        assert result.exit_code == 0
        prof = load_config().profiles.get("default")
        assert prof is not None
        assert prof.jira_url == "https://jira.example.com"
        assert prof.confluence_url is None
        assert prof.bitbucket_url is None

    def test_keep_all_when_pressing_enter(self, wizard_env: Path) -> None:
        """Pure-Enter run with seeded URLs must be non-destructive."""
        from atlassian_skills.cli.main import app
        from atlassian_skills.core.config import Config, Profile, load_config, save_config

        config = Config()
        config.profiles["default"] = Profile(
            jira_url="https://jira.seed", confluence_url="https://conf.seed", bitbucket_url="https://bb.seed"
        )
        save_config(config)

        runner = CliRunner()
        # Default for each seeded product is 's' (skip = keep as-is); for agent install all three
        # default Y. Enter × 3 (jira/conf/bb) + 'n' × 3 to explicitly decline asset installs.
        result = runner.invoke(app, ["setup"], input="\n\n\nn\nn\nn\n")

        assert result.exit_code == 0
        prof = load_config().profiles["default"]
        assert prof.jira_url == "https://jira.seed"
        assert prof.confluence_url == "https://conf.seed"
        assert prof.bitbucket_url == "https://bb.seed"

    def test_remove_clears_config_url(self, wizard_env: Path) -> None:
        from atlassian_skills.cli.main import app
        from atlassian_skills.core.config import Config, Profile, load_config, save_config

        config = Config()
        config.profiles["default"] = Profile(
            jira_url="https://jira.seed", confluence_url="https://conf.seed", bitbucket_url="https://bb.seed"
        )
        save_config(config)

        runner = CliRunner()
        # keep jira, keep conf, remove bb, skip agents
        result = runner.invoke(app, ["setup"], input=_wizard_input(jira=("k",), conf=("k",), bb=("r",)))

        assert result.exit_code == 0
        prof = load_config().profiles["default"]
        assert prof.jira_url == "https://jira.seed"
        assert prof.confluence_url == "https://conf.seed"
        assert prof.bitbucket_url is None

    def test_remove_env_sourced_url_emits_noop_warning(self, wizard_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from atlassian_skills.cli.main import app
        from atlassian_skills.core.config import load_config

        monkeypatch.setenv("ATLS_DEFAULT_BITBUCKET_URL", "https://bb.env")

        runner = CliRunner()
        # jira: not configured → default 's'; conf: same; bb: env-configured → 'r'
        result = runner.invoke(app, ["setup"], input=_wizard_input(jira=("s",), conf=("s",), bb=("r",)))

        assert result.exit_code == 0
        # env URL untouched; config still has nothing for bitbucket
        assert os.environ.get("ATLS_DEFAULT_BITBUCKET_URL") == "https://bb.env"
        prof = load_config().profiles.get("default")
        if prof is not None:
            assert prof.bitbucket_url is None
        assert "cannot permanently unset" in result.output


class TestNoTokenEcho:
    def test_token_value_never_appears_in_output(self, wizard_env: Path) -> None:
        from atlassian_skills.cli.main import app

        secret = "SECRETTOKEN-DO-NOT-LEAK-12345"
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["setup"],
            input=_wizard_input(jira=("a", "https://jira.example.com", secret)),
        )

        assert result.exit_code == 0
        assert secret not in result.output


class TestWizardRemove:
    """`[r]emove` on a config-URL product clears the keyring entry + the config URL, and never
    touches env vars or shell rc files (those are the user's manual setup)."""

    def test_remove_clears_keyring_and_config_url_without_touching_rc(
        self, wizard_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import atlassian_skills.cli.setup as setup_mod
        from atlassian_skills.cli.main import app
        from atlassian_skills.core.config import Config, Profile, load_config, save_config

        config = Config()
        config.profiles["default"] = Profile(jira_url="https://jira.example.com")
        save_config(config)
        # A pre-existing shell rc must be left untouched (the wizard no longer writes rc).
        zshrc = wizard_env / ".zshrc"
        zshrc.write_text("export JIRA_PERSONAL_TOKEN='left-alone'\n", encoding="utf-8")

        delete_calls: list[tuple[str, str]] = []

        def _fake_delete(profile: str, product: str) -> bool:
            # Simulate an entry that existed and was deleted — the helper now returns True on
            # success so the wizard only prints "cleared …" when something was actually removed.
            delete_calls.append((profile, product))
            return True

        monkeypatch.setattr(setup_mod, "_delete_token_keyring", _fake_delete)

        runner = CliRunner()
        result = runner.invoke(app, ["setup"], input=_wizard_input(jira=("r",)))

        assert result.exit_code == 0, result.output
        assert ("default", "jira") in delete_calls
        assert "cleared Jira keyring entry" in result.output
        # config URL removed; shell rc untouched.
        prof = load_config().profiles.get("default")
        if prof is not None:
            assert prof.jira_url is None
        assert zshrc.read_text(encoding="utf-8") == "export JIRA_PERSONAL_TOKEN='left-alone'\n"


class TestWizardPATIssuerHint:
    def test_jira_pat_hint_shown(self, wizard_env: Path) -> None:
        from atlassian_skills.cli.main import app

        runner = CliRunner()
        result = runner.invoke(
            app,
            ["setup"],
            input=_wizard_input(jira=("a", "https://jira.example.com", "t-jira")),
        )
        assert result.exit_code == 0
        assert "Generate a PAT" in result.output
        assert "Personal Access Tokens" in result.output

    def test_bitbucket_pat_hint_shown(self, wizard_env: Path) -> None:
        from atlassian_skills.cli.main import app

        runner = CliRunner()
        result = runner.invoke(
            app,
            ["setup"],
            input=_wizard_input(bb=("a", "https://bb.example.com", "t-bb")),
        )
        assert result.exit_code == 0
        assert "Generate a PAT" in result.output
        assert "HTTP access tokens" in result.output


# ---------------------------------------------------------------------------
# Guards: TTY (the wizard no longer aborts on fish — it doesn't write shell rc)
# ---------------------------------------------------------------------------


class TestTTYGuard:
    """No `bypass_tty_guard` fixture — we want the real guard.

    Note: `CliRunner.invoke` replaces sys.stdin with a non-TTY pipe by default, so
    patching `sys.stdin.isatty` directly would attach to the wrong object (the patch
    lives on the real stdin while the runner uses a substitute). We monkeypatch the
    module-level `_is_tty` helper instead — that's the indirection the guard uses.
    """

    def test_non_tty_stdin_exits_1(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.main as main_mod
        import atlassian_skills.cli.setup as setup_mod
        import atlassian_skills.core.config as config_mod

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(config_mod, "config_path", lambda: tmp_path / "config.toml")
        monkeypatch.setattr(setup_mod, "_is_tty", lambda: False)

        runner = CliRunner()
        result = runner.invoke(main_mod.app, ["setup"])

        assert result.exit_code == 1
        assert "interactive terminal" in result.output

    def test_tty_guard_passes_when_is_tty_true(self, wizard_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sanity: if _is_tty returns True, the guard does NOT exit — wizard proceeds.

        Uses the wizard_env fixture so the rest of the wizard has its tmp paths.
        """
        import atlassian_skills.cli.setup as setup_mod

        # Override the conftest `bypass_tty_guard` (which makes the guard a no-op) by
        # restoring the real implementation, then make _is_tty True. The wizard should
        # reach product prompts (we just feed skip × 3 + n × 2 and verify exit 0).
        from atlassian_skills.cli.setup import _ensure_interactive_terminal as real_guard

        monkeypatch.setattr(setup_mod, "_ensure_interactive_terminal", real_guard)
        monkeypatch.setattr(setup_mod, "_is_tty", lambda: True)

        from atlassian_skills.cli.main import app

        runner = CliRunner()
        # skip × 3 (jira/conf/bb) + n × 3 (decline agent installs).
        result = runner.invoke(app, ["setup"], input="s\ns\ns\nn\nn\nn\n")

        assert result.exit_code == 0
        assert "interactive terminal" not in result.output


class TestFishNoLongerAborts:
    def test_fish_runs_normally(self, wizard_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The wizard no longer writes shell rc, so fish is fine — it must NOT abort and must
        reach the product prompts."""
        monkeypatch.setenv("FISH_VERSION", "3.7.0")

        from atlassian_skills.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["setup"], input=_wizard_input())

        assert result.exit_code == 0
        assert "fish shell detected" not in result.output
        # The wizard reached the first product prompt.
        assert "[1/4]" in result.output


class TestCopilotInstall:
    """GitHub Copilot skill install — wizard prompt, --skills-only refresh, doctor display."""

    def test_wizard_install_copilot_writes_skill_md_and_instructions(self, wizard_env: Path) -> None:
        from atlassian_skills.cli.main import app

        runner = CliRunner()
        result = runner.invoke(
            app, ["setup"], input=_wizard_input(install_claude="n", install_codex="n", install_copilot="y")
        )

        assert result.exit_code == 0
        target = wizard_env / ".copilot" / "skills" / "atls" / "SKILL.md"
        assert target.exists()
        assert "installed-by: atls" in target.read_text(encoding="utf-8")
        # Routing block — Copilot CLI's user-global instructions file (mirrors AGENTS.md / CLAUDE.md)
        instructions = wizard_env / ".copilot" / "copilot-instructions.md"
        assert instructions.exists()
        content = instructions.read_text(encoding="utf-8")
        assert "ATLS-COPILOT:START" in content
        assert "ATLS-COPILOT:END" in content
        assert "atls" in content.lower()

    def test_wizard_decline_copilot_does_not_install(self, wizard_env: Path) -> None:
        from atlassian_skills.cli.main import app

        runner = CliRunner()
        result = runner.invoke(
            app, ["setup"], input=_wizard_input(install_claude="n", install_codex="n", install_copilot="n")
        )

        assert result.exit_code == 0
        assert not (wizard_env / ".copilot" / "skills" / "atls" / "SKILL.md").exists()

    def test_wizard_copilot_prompt_defaults_yes(self, wizard_env: Path) -> None:
        """Pressing Enter at the Copilot prompt installs (default `Y` — first-class with Claude/Codex)."""
        from atlassian_skills.cli.main import app

        runner = CliRunner()
        # Claude=n, Codex=n, Copilot=Enter (default Y)
        result = runner.invoke(
            app, ["setup"], input=_wizard_input(install_claude="n", install_codex="n", install_copilot="")
        )

        assert result.exit_code == 0
        assert (wizard_env / ".copilot" / "skills" / "atls" / "SKILL.md").exists()
        assert (wizard_env / ".copilot" / "copilot-instructions.md").exists()

    def test_skills_only_does_not_install_copilot_when_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`atls upgrade` (which uses --skills-only) must not surprise-install Copilot for users
        who never opted in. Only refresh Copilot if a prior install is already on disk."""
        import atlassian_skills.cli.setup as setup_mod

        asset_root = _make_asset_root(tmp_path)
        _stub_paths(monkeypatch, tmp_path, asset_root)

        runner = CliRunner()
        result = runner.invoke(setup_mod.setup_app, ["--skills-only"])

        assert result.exit_code == 0
        assert (tmp_path / ".claude" / "skills" / "atls" / "SKILL.md").exists()
        assert (tmp_path / ".codex" / "skills" / "atls" / "SKILL.md").exists()
        assert not (tmp_path / ".copilot" / "skills" / "atls" / "SKILL.md").exists()

    def test_skills_only_refreshes_copilot_when_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """If Copilot SKILL.md already exists, --skills-only must refresh it (so `atls upgrade`
        keeps it in sync with Claude/Codex)."""
        import atlassian_skills.cli.setup as setup_mod

        asset_root = _make_asset_root(tmp_path)
        _stub_paths(monkeypatch, tmp_path, asset_root)
        copilot_target = tmp_path / ".copilot" / "skills" / "atls" / "SKILL.md"
        copilot_target.parent.mkdir(parents=True)
        copilot_target.write_text("<!-- stale 0.2.6 marker -->", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(setup_mod.setup_app, ["--skills-only"])

        assert result.exit_code == 0
        assert "installed-by: atls 0.2.7" in copilot_target.read_text(encoding="utf-8")


class TestDoctorShowsCopilot:
    def test_doctor_lists_copilot_paths(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        from atlassian_skills.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["doctor"])

        assert result.exit_code == 0
        assert "Copilot config dir" in result.output
        assert "Copilot skill target" in result.output
        # When not installed, doctor must still show the status line
        assert "Copilot skill" in result.output
        assert "not installed" in result.output


# ---------------------------------------------------------------------------
# 0.2.8 — keyring-only wizard: keyring save, env-token skip, no-flip, missing dep
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, **profile_kwargs: object) -> None:
    """Write a config.toml with a single [profiles.default] for resolve tests."""
    from atlassian_skills.core.config import Config, Profile, save_config

    config = Config(profiles={"default": Profile(**profile_kwargs)})  # type: ignore[arg-type]
    save_config(config, tmp_path / "config.toml")


class TestWizardKeyring:
    """Keyring-only wizard: keyring save, env-token skip, no-flip, missing dep, headless caveat."""

    def test_keyring_set_password_args_match_reader(self, wizard_env: Path) -> None:
        """REGRESSION GUARD: the wizard must store under the exact key core.auth reads —
        keyring.set_password('atls-default', '<product>_token', token). If this drifts, the
        wizard saves a token the resolver can never find. Also persists storage='keyring'.
        """
        import sys

        from atlassian_skills.cli.main import app
        from atlassian_skills.core.config import load_config

        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = "jira-pat-xyz"  # for the final --resolve verify

        runner = CliRunner()
        with patch.dict(sys.modules, {"keyring": mock_keyring}):
            result = runner.invoke(
                app,
                ["setup"],
                input=_wizard_input(jira=("e", "https://jira.example.com", "jira-pat-xyz")),
            )

        assert result.exit_code == 0, result.output
        mock_keyring.set_password.assert_any_call("atls-default", "jira_token", "jira-pat-xyz")
        assert load_config().profiles["default"].storage == "keyring"

    def test_env_token_product_is_skipped_not_shadowed(self, wizard_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A product whose token is in the environment must NOT get a (shadowed) keyring entry —
        the wizard says the token stays in the environment and never calls set_password for it.
        The env-detection banner is shown up-front."""
        import sys

        from atlassian_skills.cli.main import app

        monkeypatch.setenv("ATLS_DEFAULT_JIRA_TOKEN", "x" * 44)

        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = "tok"
        runner = CliRunner()
        # jira: edit (url, no PAT prompt — token stays in env); conf/bb skip; decline agents.
        with patch.dict(sys.modules, {"keyring": mock_keyring}):
            result = runner.invoke(app, ["setup"], input=_wizard_input(jira=("e", "https://jira.example.com")))

        assert result.exit_code == 0, result.output
        # Env-detection banner appears, naming the variable.
        assert "already reading token(s) from your environment" in result.output
        assert "ATLS_DEFAULT_JIRA_TOKEN" in result.output
        # The wizard tells the user the token stays in the environment...
        assert "token stays in the environment" in result.output
        # ...and never writes a (shadowed) keyring entry for jira.
        for call in mock_keyring.set_password.call_args_list:
            assert call.args[1] != "jira_token", "wizard must not write a keyring entry for an env product"

    def test_pure_env_user_skip_through_does_not_flip_storage(
        self, wizard_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pure env user (env token set) who skips everything must NOT get storage flipped to
        keyring — no keyring token was written, so storage stays as-is."""
        import sys

        from atlassian_skills.cli.main import app
        from atlassian_skills.core.config import load_config

        monkeypatch.setenv("ATLS_DEFAULT_JIRA_TOKEN", "x" * 44)

        mock_keyring = MagicMock()
        runner = CliRunner()
        # Skip all products (jira default is 'e' but we pass 's'); decline agents.
        with patch.dict(sys.modules, {"keyring": mock_keyring}):
            result = runner.invoke(app, ["setup"], input=_wizard_input(jira=("s",)))

        assert result.exit_code == 0, result.output
        prof = load_config().profiles.get("default")
        assert prof is None or prof.storage != "keyring"
        mock_keyring.set_password.assert_not_called()

    def test_keyring_missing_dep_aborts_with_installer_hint(self, wizard_env: Path) -> None:
        """Entering a keyring token when the keyring package is unavailable aborts with exit 1 and
        an install hint mentioning the extra."""
        import sys

        from atlassian_skills.cli.main import app

        runner = CliRunner()
        # Enter a real PAT for jira (no env), but keyring import fails on save → abort.
        with patch.dict(sys.modules, {"keyring": None}):
            result = runner.invoke(
                app,
                ["setup"],
                input=_wizard_input(jira=("e", "https://jira.example.com", "jira-pat")),
            )

        assert result.exit_code == 1, result.output
        assert "atlassian-skills[keyring]" in result.output

    def test_keyring_backend_failure_aborts_cleanly_not_traceback(self, wizard_env: Path) -> None:
        """REGRESSION: on a headless/locked store, keyring.set_password raises
        keyring.errors.KeyringError (NOT ImportError). The wizard must catch it and exit 1 with a
        friendly message — never dump a raw traceback at the user."""
        import sys

        from atlassian_skills.cli.main import app

        class _NoKeyringError(RuntimeError):
            """Stands in for keyring.errors.NoKeyringError."""

        mock_keyring = MagicMock()
        mock_keyring.set_password.side_effect = _NoKeyringError("No recommended backend was available")

        runner = CliRunner()
        with patch.dict(sys.modules, {"keyring": mock_keyring}):
            result = runner.invoke(
                app,
                ["setup"],
                input=_wizard_input(jira=("e", "https://jira.example.com", "jira-pat")),
            )

        assert result.exit_code == 1, result.output
        assert "couldn't write to the OS keyring" in result.output
        # The friendly message points at env vars; no raw exception class leaked as a crash.
        assert result.exception is None or isinstance(result.exception, SystemExit)

    def test_command_storage_user_is_warned_before_flip(self, wizard_env: Path) -> None:
        """A profile on storage='command' must be warned up front that setting a PAT switches the
        WHOLE profile to keyring (silently breaking command resolution) — never flip silently."""
        _write_config(
            wizard_env,
            storage="command",
            credential_command="op read op://vault/atlassian/token",
            jira_url="https://jira.example.com",
        )

        from atlassian_skills.cli.main import app

        runner = CliRunner()
        # Skip every product, decline agents — no PAT entered, so nothing is flipped.
        result = runner.invoke(app, ["setup"], input=_wizard_input(jira=("s",)))

        assert result.exit_code == 0, result.output
        assert "storage='command'" in result.output
        assert "ENTIRE profile switches to storage='keyring'" in result.output.replace("\n", " ")

    def test_headless_caveat_warns_after_saving(self, wizard_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When headless signals are present and a token was saved, the wizard prints a one-line
        caveat that the keyring may be locked in this session."""
        import sys

        import atlassian_skills.cli.setup as setup_mod
        from atlassian_skills.cli.main import app

        monkeypatch.setattr(setup_mod, "_detect_headless", lambda: ["running inside Docker (/.dockerenv present)"])
        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = "jira-pat"
        runner = CliRunner()
        with patch.dict(sys.modules, {"keyring": mock_keyring}):
            result = runner.invoke(
                app,
                ["setup"],
                input=_wizard_input(jira=("e", "https://jira.example.com", "jira-pat")),
            )

        assert result.exit_code == 0, result.output
        assert "may not be able to unlock" in result.output


class TestAuthStatusResolve:
    """`render_auth_status` must not probe providers unless resolve=True."""

    def test_resolve_false_does_not_touch_keyring(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.core.config as config_mod
        from atlassian_skills.cli.auth import render_auth_status

        monkeypatch.setattr(config_mod, "config_path", lambda: tmp_path / "config.toml")
        for v in ("ATLS_DEFAULT_JIRA_TOKEN", "JIRA_PERSONAL_TOKEN"):
            monkeypatch.delenv(v, raising=False)
        _write_config(tmp_path, storage="keyring", jira_url="https://jira.example.com")

        mock_keyring = MagicMock()
        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            render_auth_status("default", resolve=False)

        mock_keyring.get_password.assert_not_called()

    def test_resolve_true_probes_keyring(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.core.config as config_mod
        from atlassian_skills.cli.auth import render_auth_status

        monkeypatch.setattr(config_mod, "config_path", lambda: tmp_path / "config.toml")
        for v in ("ATLS_DEFAULT_JIRA_TOKEN", "JIRA_PERSONAL_TOKEN"):
            monkeypatch.delenv(v, raising=False)
        _write_config(tmp_path, storage="keyring", jira_url="https://jira.example.com")

        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = "resolved-tok"
        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            render_auth_status("default", resolve=True)

        mock_keyring.get_password.assert_any_call("atls-default", "jira_token")

    def test_resolve_false_does_not_run_command(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.core.config as config_mod
        from atlassian_skills.cli.auth import render_auth_status

        monkeypatch.setattr(config_mod, "config_path", lambda: tmp_path / "config.toml")
        for v in ("ATLS_DEFAULT_JIRA_TOKEN", "JIRA_PERSONAL_TOKEN"):
            monkeypatch.delenv(v, raising=False)
        _write_config(tmp_path, storage="command", jira_command="echo should-not-run")

        with patch("atlassian_skills.core.auth.subprocess.run") as mock_run:
            render_auth_status("default", resolve=False)

        mock_run.assert_not_called()

    def test_env_shadowing_configured_keyring_is_warned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """storage=keyring but a live env var resolves first → explicit shadow warning."""
        import atlassian_skills.core.config as config_mod
        from atlassian_skills.cli.auth import render_auth_status

        monkeypatch.setattr(config_mod, "config_path", lambda: tmp_path / "config.toml")
        monkeypatch.setenv("ATLS_DEFAULT_JIRA_TOKEN", "shadowing-env-token")
        _write_config(tmp_path, storage="keyring", jira_url="https://jira.example.com")

        render_auth_status("default", resolve=False)

        out = capsys.readouterr().out
        assert "source=env" in out
        assert "shadowing it" in out
        assert "storage=keyring" in out
        assert "ATLS_DEFAULT_JIRA_TOKEN" in out

    def test_env_source_no_warning_when_storage_is_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With storage=env, source=env is expected — no shadow warning should appear."""
        import atlassian_skills.core.config as config_mod
        from atlassian_skills.cli.auth import render_auth_status

        monkeypatch.setattr(config_mod, "config_path", lambda: tmp_path / "config.toml")
        monkeypatch.setenv("ATLS_DEFAULT_JIRA_TOKEN", "env-token")
        _write_config(tmp_path, storage="env", jira_url="https://jira.example.com")

        render_auth_status("default", resolve=False)

        out = capsys.readouterr().out
        assert "source=env" in out
        assert "shadowing" not in out


class TestWizardMenuDefault:
    """Per-product menu default: `e` when the config is incomplete (token but no URL), else `k`."""

    def test_incomplete_product_defaults_to_edit(self, wizard_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from atlassian_skills.cli.main import app

        # Token present (via env) but no URL configured → incomplete → menu should default to `e`.
        monkeypatch.setenv("ATLS_DEFAULT_JIRA_TOKEN", "x" * 44)
        runner = CliRunner()
        result = runner.invoke(app, ["setup"], input=_wizard_input(jira=("s",)))

        assert result.exit_code == 0, result.output
        # The Jira block prints its menu with default=e.
        assert "[s]kip / [e]dit / [r]emove [default=e]" in result.output

    def test_complete_product_defaults_to_skip(self, wizard_env: Path) -> None:
        from atlassian_skills.cli.main import app
        from atlassian_skills.core.config import Config, Profile, save_config

        # URL configured → product is usable → menu should default to `s` (skip = keep as-is).
        save_config(
            Config(profiles={"default": Profile(jira_url="https://jira.example.com")}), wizard_env / "config.toml"
        )
        runner = CliRunner()
        result = runner.invoke(app, ["setup"], input=_wizard_input(jira=("s",)))

        assert result.exit_code == 0, result.output
        assert "[s]kip / [e]dit / [r]emove [default=s]" in result.output
