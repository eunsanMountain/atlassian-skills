from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from atlassian_skills.cli.main import app

runner = CliRunner()


class TestDetectInstallMethod:
    def test_detects_uv_unix_layout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.upgrade as upgrade_mod

        monkeypatch.setattr(sys, "executable", "/home/user/.local/share/uv/tools/atlassian-skills/bin/python")
        assert upgrade_mod._detect_install_method() == "uv"

    def test_detects_uv_windows_layout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.upgrade as upgrade_mod

        monkeypatch.setattr(
            sys,
            "executable",
            "C:/Users/user/AppData/Roaming/uv/tools/atlassian-skills/Scripts/python.exe",
        )
        assert upgrade_mod._detect_install_method() == "uv"

    def test_pip_venv_is_not_uv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.upgrade as upgrade_mod

        monkeypatch.setattr(sys, "executable", "/home/user/project/.venv/bin/python")
        assert upgrade_mod._detect_install_method() == "pip"

    def test_detects_pipx_unix_layout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.upgrade as upgrade_mod

        monkeypatch.setattr(sys, "executable", "/home/user/.local/pipx/venvs/atlassian-skills/bin/python")
        assert upgrade_mod._detect_install_method() == "pipx"

    def test_detects_pipx_windows_layout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.upgrade as upgrade_mod

        monkeypatch.setattr(
            sys,
            "executable",
            "C:/Users/user/pipx/venvs/atlassian-skills/Scripts/python.exe",
        )
        assert upgrade_mod._detect_install_method() == "pipx"

    def test_system_python_is_pip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.upgrade as upgrade_mod

        monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
        assert upgrade_mod._detect_install_method() == "pip"


class TestUpgradeUv:
    def test_uv_path_runs_uv_then_setup_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.upgrade as upgrade_mod

        commands: list[list[str]] = []

        def fake_which(name: str) -> str | None:
            return {
                "uv": "/usr/bin/uv",
                "atls": "/usr/local/bin/atls",
            }.get(name)

        def fake_run(
            command: list[str], *, text: bool, capture_output: bool, check: bool
        ) -> subprocess.CompletedProcess[str]:
            assert text is True
            assert capture_output is True
            assert check is False
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        monkeypatch.setattr(upgrade_mod, "_detect_install_method", lambda: "uv")
        monkeypatch.setattr(upgrade_mod.shutil, "which", fake_which)
        monkeypatch.setattr(upgrade_mod.subprocess, "run", fake_run)

        result = runner.invoke(app, ["upgrade"])

        assert result.exit_code == 0
        assert commands == [
            ["/usr/bin/uv", "tool", "upgrade", "atlassian-skills"],
            ["/usr/local/bin/atls", "setup", "all"],
        ]
        assert "detected uv install" in result.output
        assert "Refreshing Claude/Codex setup with `atls setup all`..." in result.output

    def test_uv_path_falls_back_to_current_executable_for_setup(
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

        def fake_run(
            command: list[str], *, text: bool, capture_output: bool, check: bool
        ) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        monkeypatch.setattr(upgrade_mod, "_detect_install_method", lambda: "uv")
        monkeypatch.setattr(upgrade_mod.shutil, "which", fake_which)
        monkeypatch.setattr(upgrade_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(sys, "argv", [str(current_atls)])

        result = runner.invoke(app, ["upgrade"])

        assert result.exit_code == 0
        assert commands[-1] == [str(current_atls), "setup", "all"]

    def test_uv_path_fails_when_uv_is_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.upgrade as upgrade_mod

        monkeypatch.setattr(upgrade_mod, "_detect_install_method", lambda: "uv")
        monkeypatch.setattr(upgrade_mod.shutil, "which", lambda _name: None)

        result = runner.invoke(app, ["upgrade"])

        assert result.exit_code == 1
        assert "Required executable `uv` was not found on PATH." in result.output


class TestUpgradePip:
    def test_pip_path_runs_pip_upgrade_then_setup_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.upgrade as upgrade_mod

        commands: list[list[str]] = []

        def fake_which(name: str) -> str | None:
            return {"atls": "/usr/local/bin/atls"}.get(name)

        def fake_run(
            command: list[str], *, text: bool, capture_output: bool, check: bool
        ) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        monkeypatch.setattr(upgrade_mod, "_detect_install_method", lambda: "pip")
        monkeypatch.setattr(upgrade_mod.shutil, "which", fake_which)
        monkeypatch.setattr(upgrade_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

        result = runner.invoke(app, ["upgrade"])

        assert result.exit_code == 0
        assert commands == [
            ["/usr/bin/python3", "-m", "pip", "install", "--upgrade", "atlassian-skills"],
            ["/usr/local/bin/atls", "setup", "all"],
        ]
        assert "detected pip install" in result.output
        assert "uv tool install atlassian-skills" in result.output  # hint shown

    def test_pip_path_does_not_require_uv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.upgrade as upgrade_mod

        which_calls: list[str] = []

        def fake_which(name: str) -> str | None:
            which_calls.append(name)
            return {"atls": "/usr/local/bin/atls"}.get(name)  # no uv entry

        def fake_run(
            command: list[str], *, text: bool, capture_output: bool, check: bool
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        monkeypatch.setattr(upgrade_mod, "_detect_install_method", lambda: "pip")
        monkeypatch.setattr(upgrade_mod.shutil, "which", fake_which)
        monkeypatch.setattr(upgrade_mod.subprocess, "run", fake_run)

        result = runner.invoke(app, ["upgrade"])

        assert result.exit_code == 0
        assert "uv" not in which_calls  # never tried to locate uv on pip path


class TestUpgradePipx:
    def test_pipx_path_runs_pipx_upgrade_then_setup_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.upgrade as upgrade_mod

        commands: list[list[str]] = []

        def fake_which(name: str) -> str | None:
            return {
                "pipx": "/usr/local/bin/pipx",
                "atls": "/usr/local/bin/atls",
            }.get(name)

        def fake_run(
            command: list[str], *, text: bool, capture_output: bool, check: bool
        ) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        monkeypatch.setattr(upgrade_mod, "_detect_install_method", lambda: "pipx")
        monkeypatch.setattr(upgrade_mod.shutil, "which", fake_which)
        monkeypatch.setattr(upgrade_mod.subprocess, "run", fake_run)

        result = runner.invoke(app, ["upgrade"])

        assert result.exit_code == 0
        assert commands == [
            ["/usr/local/bin/pipx", "upgrade", "atlassian-skills"],
            ["/usr/local/bin/atls", "setup", "all"],
        ]
        assert "detected pipx install" in result.output

    def test_pipx_path_falls_back_to_pip_when_pipx_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.cli.upgrade as upgrade_mod

        commands: list[list[str]] = []

        def fake_which(name: str) -> str | None:
            return {"atls": "/usr/local/bin/atls"}.get(name)  # pipx absent

        def fake_run(
            command: list[str], *, text: bool, capture_output: bool, check: bool
        ) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        monkeypatch.setattr(upgrade_mod, "_detect_install_method", lambda: "pipx")
        monkeypatch.setattr(upgrade_mod.shutil, "which", fake_which)
        monkeypatch.setattr(upgrade_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(sys, "executable", "/home/user/.local/pipx/venvs/atlassian-skills/bin/python")

        result = runner.invoke(app, ["upgrade"])

        assert result.exit_code == 0
        assert commands[0] == [
            "/home/user/.local/pipx/venvs/atlassian-skills/bin/python",
            "-m",
            "pip",
            "install",
            "--upgrade",
            "atlassian-skills",
        ]
        assert "pipx` is not on PATH" in result.output
