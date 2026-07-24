from __future__ import annotations

import json
import sys

import cfxmark
import pytest

from atlassian_skills.cli import main as main_module
from atlassian_skills.core.errors import ValidationError


def test_entrypoint_preserves_structured_atlas_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail() -> None:
        raise ValidationError("synthetic validation", context={"reason": "synthetic"})

    monkeypatch.setattr(main_module, "app", fail)
    monkeypatch.setattr(sys, "argv", ["atls", "--format=json", "confluence", "page", "get", "1"])

    with pytest.raises(SystemExit) as exc_info:
        main_module.entrypoint()

    captured = capsys.readouterr()
    assert exc_info.value.code == 7
    assert json.loads(captured.out)["error"]["context"]["reason"] == "synthetic"
    assert captured.err == ""
    assert "Traceback" not in captured.out


def test_entrypoint_normalizes_cfxmark_error_as_validation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail() -> None:
        raise cfxmark.ConversionError("unsafe renderer detail")

    monkeypatch.setattr(main_module, "app", fail)
    monkeypatch.setattr(sys, "argv", ["atls", "confluence", "page", "pull-md", "1"])

    with pytest.raises(SystemExit) as exc_info:
        main_module.entrypoint()

    captured = capsys.readouterr()
    assert exc_info.value.code == 7
    assert captured.out == ""
    assert "markup conversion failed" in captured.err
    assert "Traceback" not in captured.err


def test_entrypoint_redacts_unexpected_exception_message_and_locals(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail() -> None:
        secret_local = "SENSITIVE_SYNTHETIC_VALUE"
        raise RuntimeError(f"do not expose {secret_local}")

    monkeypatch.setattr(main_module, "app", fail)
    monkeypatch.setattr(sys, "argv", ["atls", "--format", "json", "version"])

    with pytest.raises(SystemExit) as exc_info:
        main_module.entrypoint()

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exc_info.value.code == 1
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["error"]["context"] == {"failure": "RuntimeError"}
    assert "SENSITIVE_SYNTHETIC_VALUE" not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err
