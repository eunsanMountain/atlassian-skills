"""Merging a description somebody else also edited, and moving authority.

The property worth the two-command split: a merge takes as long as it takes to
understand two edits, and the issue can move again while it is being read.
`finalize` refuses in that case. Without it, a merge that took ten minutes
publishes over whatever arrived in minute three -- and the person doing the
merge has no way to know.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from atlassian_skills.core.errors import ConflictError, ValidationError
from atlassian_skills.jira import description_md, description_merge
from atlassian_skills.jira.description_binding import read_binding

PLAIN = "h2. Title\n\nplain paragraph\n"


class FakeIssue:
    base_url = "https://jira.example.com"

    def __init__(self, description: str = PLAIN) -> None:
        self.description = description
        self.updated = "2026-07-29T10:00:00.000+0900"

    def get_issue_raw(self, key: str, fields: list[str] | None = None) -> dict[str, Any]:
        return {
            "id": "10001",
            "key": "DEMO600-1",
            "fields": {"description": self.description, "updated": self.updated, "attachment": []},
        }


@pytest.fixture
def managed(tmp_path: Path) -> tuple[FakeIssue, Path]:
    issue = FakeIssue()
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    return issue, path


# --------------------------------------------------------------------------
# Laying out the three sides
# --------------------------------------------------------------------------


def test_prepare_writes_all_three_sides_and_merges_nothing(managed: tuple[FakeIssue, Path]) -> None:
    """It stops on purpose. Merging prose by rule produces a document that reads
    fine and says something nobody wrote, and a reader cannot tell which
    sentences a person chose."""

    issue, path = managed
    path.write_bytes(b"# mine\n")
    issue.description = "h2. Theirs\n"

    result = description_merge.prepare_merge(issue, "DEMO600-1", path)

    assert Path(result["base"]).exists()
    assert Path(result["local"]).read_bytes() == b"# mine\n"
    assert Path(result["remote"]).read_bytes() == b"h2. Theirs\n"
    # No merged file was invented.
    assert not (Path(result["base"]).parent / "merged.txt").exists()


def test_prepare_names_the_command_that_follows_it(managed: tuple[FakeIssue, Path]) -> None:
    """A complete argv, so the caller does not compose one from the shape of
    the directory.

    Named by its visible spelling. `finalize-merge` is the same command under a hidden
    alias, and an action telling a caller what to run next has to name something
    `--help` admits exists -- otherwise the recovery is unreachable by anyone
    discovering the CLI by reading it, which is how an agent discovers it.
    `test_next_action_argv` enforces that across every action in the product.
    """

    issue, path = managed
    path.write_bytes(b"# mine\n")
    issue.description = "h2. Theirs\n"

    action = description_merge.prepare_merge(issue, "DEMO600-1", path)["next_actions"][0]

    assert action["argv"][:4] == ["jira", "issue", "description", "record-reconciled-against"]
    assert not any("<" in part for part in action["argv"])


def test_a_file_with_no_binding_has_no_base_to_merge_against(tmp_path: Path) -> None:
    path = tmp_path / "loose.md"
    path.write_bytes(b"text\n")

    with pytest.raises(ValidationError) as caught:
        description_merge.prepare_merge(FakeIssue(), "DEMO600-1", path)

    assert caught.value.context["reason"] == "description_binding_missing"


# --------------------------------------------------------------------------
# The refusal the split exists for
# --------------------------------------------------------------------------


def test_finalize_refuses_when_the_issue_moved_again_during_the_merge(
    managed: tuple[FakeIssue, Path],
) -> None:
    """The whole reason prepare and finalize are two commands.

    Without this, a merge that took ten minutes publishes over whatever arrived
    in minute three, and the person merging never learns it happened.
    """

    issue, path = managed
    path.write_bytes(b"# mine\n")
    issue.description = "h2. Theirs\n"
    prepared = description_merge.prepare_merge(issue, "DEMO600-1", path)

    merged = Path(prepared["base"]).parent / "merged.txt"
    merged.write_bytes(b"# merged by hand\n")
    # A third edit lands while the merge was being read.
    issue.description = "h2. And again\n"

    with pytest.raises(ConflictError) as caught:
        description_merge.finalize_merge(issue, "DEMO600-1", path, merged=merged)

    assert caught.value.context["reason"] == "remote_changed_since_prepare"
    # The managed file is untouched: nothing half-applied.
    assert path.read_bytes() == b"# mine\n"


def test_finalize_without_a_prepare_is_refused(managed: tuple[FakeIssue, Path]) -> None:
    """Finalizing without preparing would bind to a state nobody compared
    against, which is a two-way overwrite wearing a merge's name."""

    issue, path = managed
    merged = path.with_name("merged.txt")
    merged.write_bytes(b"# merged\n")

    with pytest.raises(ValidationError) as caught:
        description_merge.finalize_merge(issue, "DEMO600-1", path, merged=merged)

    assert caught.value.context["reason"] == "merge_not_prepared"


def test_a_finalized_merge_is_bound_to_what_it_was_merged_against(
    managed: tuple[FakeIssue, Path],
) -> None:
    """Bound to the remote the merge resolved against, so the push that follows
    does not report a conflict the merge already settled."""

    issue, path = managed
    path.write_bytes(b"# mine\n")
    issue.description = "h2. Theirs\n"
    prepared = description_merge.prepare_merge(issue, "DEMO600-1", path)
    merged = Path(prepared["base"]).parent / "merged.txt"
    merged.write_bytes(b"# merged by hand\n")

    result = description_merge.finalize_merge(issue, "DEMO600-1", path, merged=merged)

    assert result["status"] == "finalized"
    assert path.read_bytes() == b"# merged by hand\n"
    binding = read_binding(path)
    assert binding is not None
    from atlassian_skills.jira.description_binding import source_sha256

    assert binding.source_sha256 == source_sha256("h2. Theirs\n")


# --------------------------------------------------------------------------
# Which representation owns the directory
# --------------------------------------------------------------------------


def test_authority_moves_only_between_the_two_that_exist(managed: tuple[FakeIssue, Path]) -> None:
    issue, path = managed

    with pytest.raises(ValidationError) as caught:
        description_merge.set_authority(issue, "DEMO600-1", path, to="xhtml")

    assert caught.value.context["reason"] == "unknown_authority"
    assert caught.value.context["allowed"] == ["md", "wiki"]


def test_moving_authority_rebinds_against_a_fresh_read(managed: tuple[FakeIssue, Path]) -> None:
    """The file being handed authority has to be bound to something current.
    Handing it over on a stale binding is how the next push overwrites an edit
    nobody saw."""

    issue, path = managed
    issue.description = "h2. Moved on\n"
    issue.updated = "2026-07-29T12:00:00.000+0900"

    result = description_merge.set_authority(issue, "DEMO600-1", path, to="wiki")

    assert result["authority"] == "wiki"
    binding = read_binding(path)
    assert binding is not None
    assert binding.authority == "wiki"
    assert binding.remote_updated == "2026-07-29T12:00:00.000+0900"
    assert binding.base_wiki == "h2. Moved on\n"


def test_authority_cannot_be_handed_to_markdown_without_a_grade(managed: tuple[FakeIssue, Path]) -> None:
    """Moving authority to Markdown changed a field and nothing else.

    It wrote `authority="md"` with `base_wiki` set to whatever the issue holds
    and `base_markdown` carried over from the old binding -- or empty, when the
    file never had one. The publish proof reads an empty `base_markdown` as "no
    baseline to check against" and returns `safe: True` without looking at
    anything, so the next `md push` sent a file that had never been graded. A
    numbered list came back as two H1s and a table as escaped pipes, at exit 0.

    Handing authority back needs the same round trip `md pull` runs. Until that
    exists, the move is refused rather than performed unproven.
    """

    issue, path = managed

    with pytest.raises(ValidationError) as caught:
        description_merge.set_authority(issue, "DEMO600-1", path, to="md")

    assert caught.value.context["reason"] == "authority_to_md_unavailable"
    binding = read_binding(path)
    assert binding is not None
    assert binding.authority == "md"
