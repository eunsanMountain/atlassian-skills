from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from atlassian_skills.cli.setup import _inject_claude_md_block, _inject_codex_agents_block, _install


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

    def test_install_overwrites_identical(self, tmp_path: Path) -> None:
        content = "# Same content"
        source = tmp_path / "source.md"
        source.write_text(content, encoding="utf-8")
        target = tmp_path / "target.md"
        target.write_text(content, encoding="utf-8")

        msg = _install(source, target)

        assert "updated" in msg
        assert "backup" in msg
        # backup is always created for existing files
        backup = target.with_suffix(target.suffix + ".bak")
        assert backup.exists()


class TestInjectClaudeMdBlock:
    def test_creates_claude_md_if_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        # Monkey-patch the function's path resolution
        import atlassian_skills.cli.setup as setup_mod
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
        assert "# User stuff" in content  # user content preserved
        assert "updated" in msg

    def test_updates_when_identical(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.setup as setup_mod
        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        claude_md.parent.mkdir(parents=True)
        # Write the exact block that would be generated
        from atlassian_skills.cli.setup import _claude_md_block
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


class TestSetupCodex:
    def test_setup_codex_installs_skill_and_agents_block(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.setup as setup_mod

        asset_root = tmp_path / "assets"
        source_dir = asset_root / "codex"
        source_dir.mkdir(parents=True)
        (source_dir / "SKILL.md").write_text("<!-- installed-by: atls 0.1.0 -->", encoding="utf-8")

        monkeypatch.setattr(setup_mod, "ASSETS_DIR", asset_root)
        monkeypatch.setattr(setup_mod, "_get_codex_skill_target", lambda: tmp_path / ".agents" / "skills" / "atls" / "SKILL.md")
        monkeypatch.setattr(setup_mod, "_get_codex_legacy_target", lambda: tmp_path / ".codex" / "skills" / "atls" / "SKILL.md")
        monkeypatch.setattr(setup_mod, "_get_codex_agents_path", lambda: tmp_path / ".codex" / "AGENTS.md")

        runner = CliRunner()
        result = runner.invoke(setup_mod.setup_app, ["codex"])

        assert result.exit_code == 0
        assert (tmp_path / ".agents" / "skills" / "atls" / "SKILL.md").exists()
        assert (tmp_path / ".codex" / "skills" / "atls" / "SKILL.md").exists()
        agents_content = (tmp_path / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
        assert "ATLS-CODEX:START" in agents_content
        assert "ATLS Codex block" in result.output


class TestStatus:
    def test_status_not_installed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        from atlassian_skills.cli.setup import setup_app

        runner = CliRunner()
        result = runner.invoke(setup_app, ["status"])

        assert result.exit_code == 0
        assert "not installed" in result.output

    def test_status_installed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        # Create a fake installed file with version marker
        codex_target = tmp_path / ".agents" / "skills" / "atls" / "SKILL.md"
        codex_target.parent.mkdir(parents=True)
        codex_target.write_text("<!-- installed-by: atls 0.1.0 -->", encoding="utf-8")

        from atlassian_skills.cli.setup import setup_app

        runner = CliRunner()
        result = runner.invoke(setup_app, ["status"])

        assert result.exit_code == 0
        assert "v0.1.0" in result.output
        assert "Codex skill" in result.output
