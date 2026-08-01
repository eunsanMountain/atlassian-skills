"""Reading a description as managed Markdown, and refusing to when it cannot go back.

The rule this file exists to pin: a description that cannot be PUBLISHED as
Markdown is not PULLED as Markdown. Writing the file anyway is friendly in the
moment and cruel later -- somebody edits it and there is no way to get the edit
to Jira. The refusal carries the exact wiki argv instead, which is a workflow
that already works.

What may be pulled is narrow, and narrowed again since: all three
identity-bearing constructs were refused together until the round trip was
measured per kind. A smart link and an attachment reference come back byte for
byte; a mention is deleted. Bodies carrying the first two are now managed, and
what keeps that honest is the publish proving their values and counts -- so the
tests here cover both what is now let through and what still is not.

Publishing lives in `description_push`, and the last test pins that it has not
grown a second home here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from atlassian_skills.core.errors import ValidationError
from atlassian_skills.jira import description_md
from atlassian_skills.jira.description_binding import read_binding

PLAIN = "h2. Title\n\nplain paragraph\n"
MENTION = "h2. Title\n\n[~alice] please review\n"
#: Markdown written into a wiki field: `##` is a numbered-list marker there, so
#: the heading is not a heading. Zero warnings, zero losses, entirely wrong.
MARKDOWN_LOOKING = "## 방향\n# first\n# second\n"


class FakeIssue:
    base_url = "https://jira.example.com"

    def __init__(self, description: str, attachments: list[dict[str, str]] | None = None) -> None:
        self.description = description
        self.updated = "2026-07-29T10:00:00.000+0900"
        self.puts = 0
        self.attachments = attachments or []

    def get_issue_raw(self, key: str, fields: list[str] | None = None) -> dict[str, Any]:
        return {
            "id": "10001",
            "key": "DEMO600-1",
            "fields": {
                "description": self.description,
                "updated": self.updated,
                "attachment": self.attachments,
            },
        }

    def update_issue(self, *_a: Any, **_k: Any) -> None:
        self.puts += 1


# --------------------------------------------------------------------------
# Refusing to hand over a file whose edits could not be published
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "why"),
    [(MENTION, "identity_not_carried"), (MARKDOWN_LOOKING, "write_back_would_change_the_body")],
    ids=["mention", "markdown-looking"],
)
def test_an_unmanageable_description_is_not_written_as_markdown(body: str, why: str, tmp_path: Path) -> None:
    issue = FakeIssue(body)
    path = tmp_path / "description.md"

    with pytest.raises(ValidationError) as caught:
        description_md.pull_md(issue, "DEMO600-1", output_path=path)

    assert caught.value.context["reason"] == "description_not_markdown_manageable"
    assert why in caught.value.context["grade"]["reasons"]
    # Nothing on disk. A file that exists is a file somebody edits.
    assert not path.exists()


def _attachment(attachment_id: str, filename: str) -> dict[str, str]:
    return {"id": attachment_id, "filename": filename, "created": "", "size": "10"}


def test_a_reference_two_attachments_could_answer_is_not_written_as_markdown(tmp_path: Path) -> None:
    """`!diagram.png!` names a filename and nothing else.

    Jira accepts two attachments under one name, and then the reference does not
    resolve to either of them in particular. Nothing here rewrites references,
    so publishing one back would be harmless -- but a managed file whose
    references cannot be resolved is one no later attachment work could act on,
    and admitting it now would be building that debt deliberately.
    """

    issue = FakeIssue(
        "h2. Title\n\n!diagram.png!\n",
        [_attachment("42", "diagram.png"), _attachment("77", "diagram.png")],
    )
    path = tmp_path / "description.md"

    with pytest.raises(ValidationError) as caught:
        description_md.pull_md(issue, "DEMO600-1", output_path=path)

    assert caught.value.context["reason"] == "attachment_filename_ambiguous"
    assert caught.value.context["filenames"] == ["diagram.png"]
    assert not path.exists()


def test_two_attachments_with_different_names_are_not_ambiguous(tmp_path: Path) -> None:
    """The bottom of that check. Duplicate filenames are the problem, not having
    more than one attachment -- and without this, the refusal above would be
    satisfied by code that rejects every issue carrying two files."""

    issue = FakeIssue(
        "h2. Title\n\n!diagram.png!\n",
        [_attachment("42", "diagram.png"), _attachment("77", "chart.png")],
    )
    path = tmp_path / "description.md"

    result = description_md.pull_md(issue, "DEMO600-1", output_path=path)

    assert result["grade"]["status"] == "markdown_identity_bound"
    assert path.exists()


def test_an_unreferenced_duplicate_filename_does_not_refuse_the_pull(tmp_path: Path) -> None:
    """Two attachments share a name and the description mentions neither. There
    is no reference to fail to resolve, so there is nothing to refuse."""

    issue = FakeIssue(
        "h2. Title\n\nplain paragraph\n",
        [_attachment("42", "spare.png"), _attachment("77", "spare.png")],
    )
    path = tmp_path / "description.md"

    result = description_md.pull_md(issue, "DEMO600-1", output_path=path)

    assert result["grade"]["status"] == "markdown_ready"


def test_the_refusal_carries_an_argv_that_can_be_run_unchanged(tmp_path: Path) -> None:
    """A complete argv, not a template. A placeholder is what makes a caller
    compose their own command, which is the thing the run-what-is-returned rule
    exists to prevent."""

    issue = FakeIssue(MENTION)
    path = tmp_path / "description.md"

    with pytest.raises(ValidationError) as caught:
        description_md.pull_md(issue, "DEMO600-1", output_path=path)

    action = caught.value.context["next_actions"][0]
    assert action["argv"][:5] == ["jira", "issue", "description", "wiki", "pull"]
    assert "DEMO600-1" in action["argv"]
    # No angle brackets anywhere: those are the shape of a hole left for a human.
    assert not any("<" in part for part in action["argv"])


# --------------------------------------------------------------------------
# When it is manageable
# --------------------------------------------------------------------------


def test_a_manageable_description_is_written_with_its_grade(tmp_path: Path) -> None:
    issue = FakeIssue(PLAIN)
    path = tmp_path / "description.md"

    result = description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)

    assert result["grade"]["status"] == "markdown_ready"
    assert path.exists()
    binding = read_binding(path)
    assert binding is not None
    assert binding.authority == "md"
    assert binding.grade == "markdown_ready"


def test_the_binding_keeps_the_wiki_it_came_from_as_well_as_the_markdown(tmp_path: Path) -> None:
    """Both bases, not one. A merge on the Markdown side needs the Markdown
    base; republishing needs the wiki the file was derived from. Keeping only
    one means recomputing the other, and a recomputed base is not the base."""

    issue = FakeIssue(PLAIN)
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path)

    binding = read_binding(path)
    assert binding is not None
    assert binding.base_wiki == PLAIN
    assert binding.base_markdown == path.read_bytes().decode("utf-8")


def test_the_hash_is_over_the_wiki_not_the_markdown(tmp_path: Path) -> None:
    """Staleness is a question about the REMOTE, and the remote holds wiki. A
    binding hashing the derived Markdown would go stale whenever the converter
    changed, which is not the same event at all."""

    from atlassian_skills.jira.description_binding import source_sha256

    issue = FakeIssue(PLAIN)
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path)

    binding = read_binding(path)
    assert binding is not None
    assert binding.source_sha256 == source_sha256(PLAIN)


# --------------------------------------------------------------------------
# What the file can still do
# --------------------------------------------------------------------------


def test_validate_reports_which_representation_owns_the_directory(tmp_path: Path) -> None:
    """Finding out at push time that the wiki side is authoritative is finding
    out too late."""

    issue = FakeIssue(PLAIN)
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path)

    report = description_md.validate_md(path)
    assert report["markdown_is_authoritative"] is True
    assert report["edited"] is False

    path.write_bytes(b"# edited\n")
    assert description_md.validate_md(path)["edited"] is True


def test_an_unbound_markdown_file_cannot_be_pushed(tmp_path: Path) -> None:
    """Unlike the wiki path, Markdown with no binding has nothing to publish
    FROM -- the wiki it was derived from is the thing that would go back."""

    path = tmp_path / "loose.md"
    path.write_bytes(b"# hand written\n")

    assert description_md.validate_md(path)["can_push"] is False


def test_diff_separates_a_local_edit_from_a_remote_one(tmp_path: Path) -> None:
    issue = FakeIssue(PLAIN)
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path)

    path.write_bytes(b"# mine\n")
    issue.description = "h2. Theirs\n"

    report = description_md.diff_md(issue, "DEMO600-1", path)
    assert report["local_edited"] is True
    assert report["remote_changed_since_pull"] is True


# --------------------------------------------------------------------------
# One publish, in one place
# --------------------------------------------------------------------------


def test_the_read_module_carries_no_publish_of_its_own() -> None:
    """Publishing lives in `description_push`, and only there.

    This asserted that no Markdown publish existed anywhere, back when none did.
    It is kept with its meaning changed because the risk changed rather than
    went away: the reading module holds a converted body and a binding, which is
    most of what a publish needs, so a second write path could grow here without
    anyone deciding to build one -- and then two publishes would be proving
    candidates against two different baselines.
    """

    assert not hasattr(description_md, "push_md")
    assert set(description_md.__all__) == {"diff_md", "pull_md", "validate_md"}
