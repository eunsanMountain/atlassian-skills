from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from atlassian_skills.cli.main import app

runner = CliRunner()


class TestUpgrade:
    def test_upgrade_runs_uv_then_setup_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.upgrade as upgrade_mod

        commands: list[list[str]] = []

        def fake_which(name: str) -> str | None:
            return {
                "uv": "/usr/bin/uv",
                "atls": "/usr/local/bin/atls",
            }.get(name)

        def fake_run(command: list[str], *, text: bool, capture_output: bool, check: bool) -> subprocess.CompletedProcess[str]:
            assert text is True
            assert capture_output is True
            assert check is False
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        monkeypatch.setattr(upgrade_mod.shutil, "which", fake_which)
        monkeypatch.setattr(upgrade_mod.subprocess, "run", fake_run)

        result = runner.invoke(app, ["upgrade"])

        assert result.exit_code == 0
        assert commands == [
            ["/usr/bin/uv", "tool", "upgrade", "atlassian-skills"],
            ["/usr/local/bin/atls", "setup", "all"],
        ]
        assert "Upgrading `atlassian-skills` with `uv tool upgrade`..." in result.output
        assert "Refreshing Claude/Codex setup with `atls setup all`..." in result.output

    def test_upgrade_falls_back_to_current_executable_for_setup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import atlassian_skills.cli.upgrade as upgrade_mod

        current_atls = tmp_path / "atls"
        current_atls.write_text("#!/bin/sh\n", encoding="utf-8")

        commands: list[list[str]] = []

        def fake_which(name: str) -> str | None:
            if name == "uv":
                return "/usr/bin/uv"
            return None

        def fake_run(command: list[str], *, text: bool, capture_output: bool, check: bool) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        monkeypatch.setattr(upgrade_mod.shutil, "which", fake_which)
        monkeypatch.setattr(upgrade_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(sys, "argv", [str(current_atls)])

        result = runner.invoke(app, ["upgrade"])

        assert result.exit_code == 0
        assert commands[-1] == [str(current_atls), "setup", "all"]

    def test_upgrade_fails_when_uv_is_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.upgrade as upgrade_mod

        monkeypatch.setattr(upgrade_mod.shutil, "which", lambda _name: None)

        result = runner.invoke(app, ["upgrade"])

        assert result.exit_code == 1
        assert "Required executable `uv` was not found on PATH." in result.output
