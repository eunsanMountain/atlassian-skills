from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from atlassian_skills.cli.main import app
from atlassian_skills.core.errors import ExitCode

runner = CliRunner()


def test_editable_pull_requires_output_before_client_or_state_access(monkeypatch: pytest.MonkeyPatch) -> None:
    make_client = MagicMock()
    monkeypatch.setattr("atlassian_skills.cli.confluence._make_client", make_client)

    result = runner.invoke(app, ["confluence", "page", "pull-md", "123456", "--format", "json"])

    assert result.exit_code == 2
    make_client.assert_not_called()


def test_pull_help_marks_output_as_required() -> None:
    result = runner.invoke(app, ["confluence", "page", "pull-md", "--help"])

    assert result.exit_code == 0, result.output
    output_line = next(line for line in result.output.splitlines() if "--output" in line)
    assert "required" in output_line.lower()


def test_needs_migration_is_successful_artifact_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "page.md"
    monkeypatch.setattr("atlassian_skills.cli.confluence._make_client", lambda _: object())
    monkeypatch.setattr(
        "atlassian_skills.confluence.pull_md.pull_md",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="pulled_with_migrations",
            markdown="# Draft\n",
            version=7,
            title="Synthetic Page",
            assets=(),
            edit_guidance=({"kind": "full_migration"},),
            warnings=(),
            losses=("manual replacement required",),
            blockers=({"code": "inline-opaque-unsupported"},),
            migration_report={"schema": "cfxmark-migration-report-v1", "occurrences": []},
            migration_report_sha256="sha256:" + "a" * 64,
            push_safe=False,
        ),
    )

    result = runner.invoke(
        app,
        ["confluence", "page", "pull-md", "123456", "--output", str(output), "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "pulled_with_migrations"
    assert payload["edit_guidance"] == [{"kind": "full_migration"}]
    assert payload["conversion"]["blockers"] == [{"code": "inline-opaque-unsupported"}]


def test_human_pull_does_not_emit_legacy_state_instructions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "page.md"
    monkeypatch.setattr("atlassian_skills.cli.confluence._make_client", lambda _: object())
    monkeypatch.setattr(
        "atlassian_skills.confluence.pull_md.pull_md",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="ready_to_edit",
            markdown="# Draft\n",
            version=7,
            title="Synthetic Page",
            warnings=(),
            losses=(),
            blockers=(),
            migration_report={"schema": "cfxmark-migration-report-v1", "occurrences": []},
            migration_report_sha256="sha256:" + "a" * 64,
            push_safe=True,
        ),
    )

    result = runner.invoke(
        app,
        ["confluence", "page", "pull-md", "123456", "--output", str(output)],
    )

    assert result.exit_code == 0, result.output
    assert "atls state" not in result.output
    assert "binding" not in result.stderr.lower()


def test_missing_binding_validate_local_is_exit_7(tmp_path: Path) -> None:
    # A file without a binding marker is a missing-binding input error (exit 7),
    # reported distinctly from an actual remote UNRESOLVED_MIGRATION so callers
    # do not chase a nonexistent migration.
    managed = tmp_path / "page.md"
    managed.write_text("# Draft\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["confluence", "page", "validate-local", str(managed), "--format", "json"],
    )

    assert result.exit_code == ExitCode.VALIDATION
    payload = json.loads(result.stdout)
    assert payload["error"]["exit_code"] == ExitCode.VALIDATION
    assert payload["error"]["code"] == "VALIDATION"
    assert payload["error"]["context"]["reason"] == "missing_managed_manifest"


def test_exit_code_constants_match_pull_first_contract() -> None:
    assert ExitCode.NOT_FOUND == 2
    assert ExitCode.PERMISSION == 3
    assert ExitCode.CONFLICT == 4
    assert ExitCode.STALE == 5
    assert ExitCode.VALIDATION == 7


def test_table_style_cli_is_removed() -> None:
    result = runner.invoke(app, ["confluence", "page", "--help"])

    assert result.exit_code == 0, result.output
    assert "table-style" not in result.output


def test_patch_text_cli_routes_find_replace_version_and_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    client = object()
    call = MagicMock(
        return_value={
            "status": "dry_run",
            "patchable": True,
            "match_count": 1,
            "page_id": "123456",
            "version": 7,
            "node_path": "/root[1]/p[1]/text()[1]",
            "before": "2026.07.07",
            "after": "2026.07.08",
        }
    )
    monkeypatch.setattr("atlassian_skills.cli.confluence._make_client", lambda _ctx: client)
    monkeypatch.setattr("atlassian_skills.confluence.patch_text.patch_text", call)

    result = runner.invoke(
        app,
        [
            "confluence",
            "page",
            "patch-text",
            "123456",
            "--find",
            "2026.07.07",
            "--replace",
            "2026.07.08",
            "--if-version",
            "7",
            "--dry-run",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["patchable"] is True
    call.assert_called_once()
    args, kwargs = call.call_args
    assert args == (client, "123456")
    assert kwargs["old"] == "2026.07.07"
    assert kwargs["new"] == "2026.07.08"
    assert kwargs["if_version"] == 7
    assert kwargs["dry_run"] is True
    assert kwargs["patch_document"] is None
    assert kwargs["reason"] is None
    assert kwargs["minor_edit"] is False


def test_patch_text_cli_routes_exact_patch_file_reason_and_minor_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object()
    call = MagicMock(return_value={"status": "dry_run", "patchable": True, "change_count": 1})
    patch_file = tmp_path / "patch.json"
    patch_file.write_text(
        json.dumps(
            {
                "version": 7,
                "changes": [
                    {
                        "node_path": "/root[1]/p[1]/text()[1]",
                        "before_fingerprint": "sha256:" + "a" * 64,
                        "after_text": "2026.07.08",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("atlassian_skills.cli.confluence._make_client", lambda _ctx: client)
    monkeypatch.setattr("atlassian_skills.confluence.patch_text.patch_text", call)

    result = runner.invoke(
        app,
        [
            "confluence",
            "page",
            "patch-text",
            "123456",
            "--patch-file",
            str(patch_file),
            "--reason",
            "Correct the published date",
            "--minor-edit",
            "--dry-run",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    _, kwargs = call.call_args
    assert kwargs["old"] is None
    assert kwargs["new"] is None
    assert kwargs["if_version"] is None
    assert kwargs["patch_document"].version == 7
    assert kwargs["patch_document"].changes[0].node_path == "/root[1]/p[1]/text()[1]"
    assert kwargs["reason"] == "Correct the published date"
    assert kwargs["minor_edit"] is True
