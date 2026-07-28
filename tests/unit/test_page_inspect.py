from __future__ import annotations

import pytest

from atlassian_skills.confluence.models import Attachment, Page, PageVersion
from atlassian_skills.confluence.page_inspect import inspect_page
from atlassian_skills.core.errors import ValidationError


class FakeClient:
    def __init__(self) -> None:
        self.reads = 0

    def get_page(self, page_id: str) -> Page:
        self.reads += 1
        return Page(
            id=page_id,
            title="Rich page",
            version=PageVersion(number=4),
            body_storage=(
                '<table><tbody><tr><td data-highlight-colour="#ff0000"><p>x</p></td></tr></tbody></table>'
                '<ac:structured-macro ac:name="future"><ac:plain-text-body><![CDATA[y]]></ac:plain-text-body>'
                "</ac:structured-macro>"
            ),
        )

    def list_attachments(self, _page_id: str) -> list[Attachment]:
        self.reads += 1
        return [Attachment(id="10", title="diagram.png")]


@pytest.mark.parametrize(
    ("intent", "workflow", "preferred_proof"),
    [
        ("read", "page-get-md", None),
        ("text-edit", "patch-text", None),
        ("append", "pull-md", "exact_remote_prefix_append"),
        ("structure-edit", "pull-md", None),
        ("presentation-edit", "web-editor", None),
    ],
)
def test_page_inspect_recommends_without_writing_or_creating_state(
    intent: str,
    workflow: str,
    preferred_proof: str | None,
) -> None:
    client = FakeClient()

    result = inspect_page(client, "123", intent=intent)

    assert result["recommended_workflow"] == workflow
    assert result.get("preferred_proof") == preferred_proof
    assert result["features"] == {
        "tables": 1,
        "styled_cells": 1,
        "aligned_cells": 0,
        "duplicate_table_shapes": 0,
        "macros": 1,
        "attachments": 1,
    }
    assert result["migration"]["consent_required"] is False
    assert result["migration"]["unsupported"] == 0
    assert result["migration"]["report"]["occurrences"][0]["effect"] == "preserved"
    assert result["managed_artifact_created"] is False
    assert client.reads == 2


class AlignedTablesClient:
    """A page whose only styling is Markdown table alignment, twice at the same shape.

    This is the shape an in-place table edit fails on: table presentation is
    re-attached by table identity, and two tables of the same shape make that
    identity ambiguous.
    """

    def get_page(self, page_id: str) -> Page:
        cell = '<td style="text-align: right;"><p>x</p></td>'
        table = f"<table><tbody><tr>{cell}</tr></tbody></table>"
        return Page(id=page_id, title="Aligned", version=PageVersion(number=1), body_storage=table + table)

    def list_attachments(self, _page_id: str) -> list[Attachment]:
        return []


def test_inspect_reports_alignment_and_duplicate_shapes_not_just_background() -> None:
    """`styled_cells` counts background only, so it reported 0 for exactly the pages
    an in-place table edit dies on. The two added counters make that visible without
    changing what `styled_cells` means."""

    result = inspect_page(AlignedTablesClient(), "123", intent="structure-edit")

    assert result["features"]["styled_cells"] == 0
    assert result["features"]["aligned_cells"] == 2
    assert result["features"]["duplicate_table_shapes"] == 2


def test_inspect_predicts_in_place_block_for_edit_intents_only() -> None:
    """Inspect is the only thing an operator has *before* pulling, so it must answer
    "can this be edited in place?" with the same prediction pull uses. Read and append
    do not pay for it — read writes nothing and append has its own proof path."""

    client = AlignedTablesClient()

    blocked = inspect_page(client, "123", intent="structure-edit")
    assert blocked["edit_guidance"][0]["kind"] == "in_place_blocked"
    assert blocked["edit_guidance"][0]["action"] == "append_or_patch_text"
    assert blocked["edit_guidance"][0]["codes"] == ["table-presentation-ambiguous"]

    for intent in ("read", "append"):
        assert "edit_guidance" not in inspect_page(client, "123", intent=intent)


def test_page_inspect_rejects_invalid_intent_before_remote_read() -> None:
    client = FakeClient()

    with pytest.raises(ValidationError) as exc_info:
        inspect_page(client, "123", intent="delete")

    assert exc_info.value.context["reason"] == "invalid_page_inspect_intent"
    assert client.reads == 0
