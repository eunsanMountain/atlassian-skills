from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from atlassian_skills.cli.main import app
from atlassian_skills.confluence.pull_md import pull_md
from atlassian_skills.confluence.push_md import push_md
from atlassian_skills.core.errors import ValidationError
from tests.unit.test_state_free_body_write import BodyClient

runner = CliRunner()


def test_managed_push_dry_run_exposes_semantic_proof_without_put(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = BodyClient()
    managed = tmp_path / "page.md"
    pull_md(client, "123", output_path=managed, portable=True, no_assets=True)
    managed.write_text(managed.read_text(encoding="utf-8").replace("B", "Edited"), encoding="utf-8")
    monkeypatch.setattr("atlassian_skills.cli.confluence._make_client", lambda _ctx: client)

    result = runner.invoke(
        app,
        [
            "confluence",
            "page",
            "push-md",
            "123",
            "--md-file",
            str(managed),
            "--dry-run",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "ready_to_publish"
    assert payload["dry_run"] is True
    assert payload["body"]["dirty"] is True
    assert payload["ownership"]["intended_operation_ids"]
    assert payload["ownership"]["unclassified"] == []
    assert client.puts == 0


@pytest.mark.parametrize("accepted", [None, "mig_sha256:" + "0" * 64])
def test_managed_push_invalid_consent_exits_7_with_approval_gated_action_and_zero_put(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    accepted: str | None,
) -> None:
    client = BodyClient()
    client.storage = '<p><ac:emoticon ac:name="smile"/></p><p>Base</p>'
    managed = tmp_path / "page.md"
    pull_md(client, "123", output_path=managed, portable=True, no_assets=True)
    managed.write_text(managed.read_text(encoding="utf-8").replace("Base", "Edited"), encoding="utf-8")
    monkeypatch.setattr("atlassian_skills.cli.confluence._make_client", lambda _ctx: client)
    argv = [
        "confluence",
        "page",
        "push-md",
        "123",
        "--md-file",
        str(managed),
        "--if-version",
        "7",
        "--reason",
        "Caller supplied reason",
        "--minor-edit",
        "--format",
        "json",
    ]
    if accepted is not None:
        argv.extend(("--accept-migration", accepted))

    result = runner.invoke(app, argv)

    assert result.exit_code == 7, result.output
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "MIGRATION_CONSENT_REQUIRED"
    context = payload["error"]["context"]
    assert context["reason"] == "migration_consent_required"
    [action] = context["next_actions"]
    assert action["id"] == "retry_with_consent"
    assert action["requires_user_approval"] is True
    assert action["description_code"] == "REVIEW_MIGRATION_AND_RETRY"
    assert action["argv"][:5] == ["atls", "confluence", "page", "push-md", "123"]
    assert action["argv"][-2:] == ["--accept-migration", context["migration_fingerprint"]]
    assert action["argv"][action["argv"].index("--reason") + 1] == "Caller supplied reason"
    assert "Page" not in action["argv"]
    assert client.puts == 0


def test_pending_operation_recovery_still_exits_7_with_safe_consent_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlassian_skills.confluence.managed_operation import insert_managed_operation, operation_for_preflight
    from atlassian_skills.confluence.migration_preflight import build_managed_preflight

    client = BodyClient()
    client.storage = '<p><ac:emoticon ac:name="smile"/></p><p>Base</p>'
    managed = tmp_path / "page.md"
    pull_md(client, "123", output_path=managed, portable=True, no_assets=True)
    managed.write_text(managed.read_text(encoding="utf-8").replace("Base", "Edited"), encoding="utf-8")
    preflight = build_managed_preflight(client, "123", managed)
    marked = insert_managed_operation(managed.read_text(encoding="utf-8"), operation_for_preflight(preflight))
    managed.write_text(marked, encoding="utf-8")
    monkeypatch.setattr("atlassian_skills.cli.confluence._make_client", lambda _ctx: client)

    result = runner.invoke(
        app,
        [
            "confluence",
            "page",
            "push-md",
            "123",
            "--md-file",
            str(managed),
            "--if-version",
            "7",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 7, result.output
    context = json.loads(result.stdout)["error"]["context"]
    assert context["reason"] == "migration_consent_required"
    [action] = context["next_actions"]
    assert action["argv"][:5] == ["atls", "confluence", "page", "push-md", "123"]
    assert action["argv"][-2:] == ["--accept-migration", context["migration_fingerprint"]]
    assert "Page" not in action["argv"]
    assert client.puts == 0
    assert "atls:operation" in managed.read_text(encoding="utf-8")


def test_managed_push_human_consent_output_shows_loss_then_alternative_then_quoted_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = BodyClient()
    client.storage = '<p><ac:emoticon ac:name="smile"/></p><p>Base</p>'
    managed = tmp_path / "page with space.md"
    pull_md(client, "123", output_path=managed, portable=True, no_assets=True)
    managed.write_text(managed.read_text(encoding="utf-8").replace("Base", "Edited"), encoding="utf-8")
    monkeypatch.setattr("atlassian_skills.cli.confluence._make_client", lambda _ctx: client)

    result = runner.invoke(
        app,
        [
            "confluence",
            "page",
            "push-md",
            "123",
            "--md-file",
            str(managed),
            "--reason",
            "Caller supplied reason",
        ],
    )

    assert result.exit_code == 7, result.output
    summary_at = result.output.index("Loss summary:")
    detail_at = result.output.index("Loss detail 1:")
    alternative_at = result.output.index("Alternative:")
    retry_at = result.output.index("Retry:")
    assert summary_at < detail_at < alternative_at < retry_at
    # The consent line shows the atls-authored value-free curated description plus the
    # stable code for traceability, NOT cfxmark's display_label sentence (which is set
    # to the raw diagnostic.message and can embed arbitrary content). The display_label
    # stays redacted from the consent envelope.
    assert "Confluence emoticon converted to a Unicode character" in result.output
    assert "emoticon-to-unicode" in result.output
    assert "rendered as Unicode text" not in result.output  # cfxmark display_label sentence absent
    assert "\n\nRetry:" in result.output
    assert "--md-file '" in result.output
    assert "--reason 'Caller supplied reason'" in result.output
    assert "Page" not in result.output
    assert result.output.rstrip().splitlines()[-1].startswith("Retry: atls confluence page push-md 123 ")
    assert client.puts == 0


def test_consent_detail_renderer_exposes_available_human_fields() -> None:
    from atlassian_skills.cli.confluence import _consent_loss_details

    [detail] = _consent_loss_details(
        {
            "migration_report": {
                "occurrences": [
                    {
                        "code": "example-loss",
                        "display_label": "Example loss",
                        "before_summary": "remote form",
                        "after_summary": "Markdown form",
                        "user_impact": "Formatting changes",
                        "suggested_workflow": "Review the affected block",
                    }
                ]
            }
        }
    )

    assert "Loss detail 1: Example loss" in detail
    assert "Impact: Formatting changes" in detail
    assert "Change: remote form -> Markdown form" in detail
    assert "Suggested: Review the affected block" in detail


def test_consent_detail_uses_curated_description_and_falls_back_to_code() -> None:
    from atlassian_skills.cli.confluence import _consent_loss_details

    # A known stable code (as it appears in the value-free consent envelope: no
    # display_label) renders the atls-authored curated description plus the code.
    [known] = _consent_loss_details({"migration_report": {"occurrences": [{"code": "emoticon-to-unicode"}]}})
    assert "Confluence emoticon converted to a Unicode character" in known
    assert "emoticon-to-unicode" in known

    # An unmapped/synthetic code falls back to the raw stable code.
    [unknown] = _consent_loss_details({"migration_report": {"occurrences": [{"code": "synthetic-unmapped-code"}]}})
    assert "Loss detail 1: synthetic-unmapped-code" in unknown


@pytest.mark.parametrize("legacy_flag", ["--attachment", "--asset-dir", "--attachment-if-exists"])
def test_managed_push_help_removes_legacy_attachment_flags(legacy_flag: str) -> None:
    result = runner.invoke(app, ["confluence", "page", "push-md", "--help"])

    assert result.exit_code == 0, result.output
    assert legacy_flag not in result.output


@pytest.mark.parametrize(
    ("attachments", "attachment_if_exists"),
    [
        ([Path("legacy.png")], None),
        (None, "skip"),
    ],
)
def test_managed_push_api_rejects_legacy_attachment_arguments(
    tmp_path: Path,
    attachments: list[Path] | None,
    attachment_if_exists: str | None,
) -> None:
    client = BodyClient()
    managed = tmp_path / "page.md"
    pull_md(client, "123", output_path=managed, portable=True, no_assets=True)

    with pytest.raises(ValidationError) as exc_info:
        push_md(
            client,
            "123",
            managed.read_text(encoding="utf-8"),
            attachments=attachments,
            attachment_if_exists=attachment_if_exists,
            managed_path=managed,
        )

    assert exc_info.value.context["reason"] == "managed_asset_flags_removed"
