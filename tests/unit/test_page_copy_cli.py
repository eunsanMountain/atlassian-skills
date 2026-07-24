from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

import atlassian_skills.cli.confluence as confluence_cli
from atlassian_skills.cli.main import app

runner = CliRunner()


def test_page_copy_help_exposes_safe_copy_contract() -> None:
    result = runner.invoke(app, ["confluence", "page", "copy", "--help"])

    assert result.exit_code == 0
    assert "--parent-id" in result.stdout
    assert "--space" in result.stdout
    assert "--include-attachments" in result.stdout
    assert "--verify" in result.stdout
    assert "--reason" in result.stdout
    assert "--dry-run" in result.stdout


def test_page_copy_cli_routes_all_copy_options(monkeypatch: pytest.MonkeyPatch) -> None:
    client = object()
    captured: dict[str, Any] = {}

    def fake_copy_page(received_client: object, source_page_id: str, **kwargs: Any) -> dict[str, Any]:
        captured.update({"client": received_client, "source_page_id": source_page_id, **kwargs})
        return {
            "status": "dry_run",
            "source": {"id": source_page_id, "version": 7, "title": "Source"},
            "target": {"space": "DST", "parent_id": "parent-1", "title": "Copied"},
            "attachments": {"count": 1, "total_bytes": 12},
            "verify": True,
            "not_copied": ["history"],
        }

    monkeypatch.setattr(confluence_cli, "_make_client", lambda _: client)
    monkeypatch.setattr(confluence_cli, "copy_confluence_page", fake_copy_page)

    result = runner.invoke(
        app,
        [
            "confluence",
            "page",
            "copy",
            "source-1",
            "--parent-id",
            "parent-1",
            "--space",
            "DST",
            "--title",
            "Copied",
            "--include-attachments",
            "--verify",
            "--reason",
            "Live validation baseline",
            "--dry-run",
            "--format=json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "dry_run"
    assert captured == {
        "client": client,
        "source_page_id": "source-1",
        "destination_parent_id": "parent-1",
        "destination_space": "DST",
        "title": "Copied",
        "include_attachments": True,
        "verify": True,
        "reason": "Live validation baseline",
        "dry_run": True,
    }
