"""Tests for cli/doctor.py — diagnostic command."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from atlassian_skills.cli.main import app


@pytest.fixture(autouse=True)
def _offline_pypi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `doctor`'s top-of-output PyPI check offline by default so the suite never hits the
    network. Tests that exercise the update banner re-patch this with a concrete value."""
    monkeypatch.setattr("atlassian_skills.cli.version.latest_pypi_version", lambda timeout=2.0: None)


class TestDoctor:
    def test_doctor_runs_with_no_install(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.core.config as config_mod

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(config_mod, "config_path", lambda: tmp_path / "config.toml")
        # Clear any host env that would mask the 'NOT SET' state
        for var in (
            "JIRA_PERSONAL_TOKEN",
            "CONFLUENCE_PERSONAL_TOKEN",
            "BITBUCKET_TOKEN",
            "ATLS_DEFAULT_JIRA_TOKEN",
            "ATLS_DEFAULT_CONFLUENCE_TOKEN",
            "ATLS_DEFAULT_BITBUCKET_TOKEN",
            "ATLS_DEFAULT_JIRA_URL",
            "ATLS_DEFAULT_CONFLUENCE_URL",
            "ATLS_DEFAULT_BITBUCKET_URL",
        ):
            monkeypatch.delenv(var, raising=False)

        runner = CliRunner()
        result = runner.invoke(app, ["doctor"])

        assert result.exit_code == 0
        assert "Platform:" in result.output
        assert "Paths:" in result.output
        assert "Skill installation status:" in result.output
        assert "Auth:" in result.output
        assert "not installed" in result.output  # no skill files exist
        assert "NOT SET" in result.output  # no token env vars

    def test_doctor_shows_installed_version_marker(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.core.config as config_mod

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(config_mod, "config_path", lambda: tmp_path / "config.toml")
        # Plant a Claude skill with a version marker
        skill = tmp_path / ".claude" / "skills" / "atls" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("<!-- installed-by: atls 0.2.7 -->", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(app, ["doctor"])

        assert result.exit_code == 0
        assert "v0.2.7" in result.output

    def test_doctor_shows_url_and_token_sources(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.core.config as config_mod

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(config_mod, "config_path", lambda: tmp_path / "config.toml")
        # Plant a config.toml with one URL + an env token
        from atlassian_skills.core.config import Config, Profile, save_config

        cfg = Config()
        cfg.profiles["default"] = Profile(jira_url="https://jira.dr-test")
        save_config(cfg)
        monkeypatch.setenv("JIRA_PERSONAL_TOKEN", "ABCDEF1234")

        runner = CliRunner()
        result = runner.invoke(app, ["doctor"])

        assert result.exit_code == 0
        assert "https://jira.dr-test" in result.output
        assert "config" in result.output
        assert "length=10" in result.output


class TestDoctorUpdateCheck:
    """The PyPI freshness banner shown at the top of `doctor`."""

    def test_up_to_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from atlassian_skills import __version__

        monkeypatch.setattr("atlassian_skills.cli.version.latest_pypi_version", lambda timeout=2.0: __version__)
        result = CliRunner().invoke(app, ["doctor"])

        assert result.exit_code == 0
        assert "up to date" in result.output
        assert __version__ in result.output

    def test_update_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("atlassian_skills.cli.version.latest_pypi_version", lambda timeout=2.0: "99.99.99")
        result = CliRunner().invoke(app, ["doctor"])

        assert result.exit_code == 0
        assert "Update available" in result.output
        assert "99.99.99" in result.output
        assert "atls upgrade" in result.output

    def test_offline_is_non_fatal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # autouse fixture already makes latest_pypi_version return None
        result = CliRunner().invoke(app, ["doctor"])

        assert result.exit_code == 0
        assert "couldn't reach PyPI" in result.output

    def test_no_update_check_skips_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import MagicMock

        probe = MagicMock()
        monkeypatch.setattr("atlassian_skills.cli.version.latest_pypi_version", probe)
        result = CliRunner().invoke(app, ["doctor", "--no-update-check"])

        assert result.exit_code == 0
        assert "update check skipped" in result.output
        probe.assert_not_called()
