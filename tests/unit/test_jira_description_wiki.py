"""The exact-wiki description workflow: what it refuses, and when.

This path converts nothing, so there is no candidate to prove and no loss to
consent to. What is left is the part every representation shares and the part
worth testing: does it notice that the issue moved, and does it check what
actually landed?

`updated` alone cannot answer the first. It moves for reasons unrelated to the
description -- an attachment upload does it -- and two writes inside its
resolution are indistinguishable. So the tests below drive the case that
separates a real stale check from a plausible one: the description changed and
`updated` did not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from atlassian_skills.core.errors import ConflictError, ValidationError
from atlassian_skills.jira import description_wiki
from atlassian_skills.jira.description_binding import binding_path, read_binding

STORED = "h2. Title\n\n* one\n* two\n"


class FakeIssue:
    """A Jira issue that records what was written to it."""

    def __init__(self, description: str = STORED, *, issue_id: str = "10001") -> None:
        self.description = description
        self.issue_id = issue_id
        self.key = "DEMO600-1"
        self.updated = "2026-07-29T10:00:00.000+0900"
        self.attachments: list[dict[str, Any]] = []
        self.puts = 0
        #: Set to rewrite on save, the way a server that normalises would.
        self.on_write: Any = None

    base_url = "https://jira.example.com"

    def get_issue_raw(self, key: str, fields: list[str] | None = None) -> dict[str, Any]:
        return {
            "id": self.issue_id,
            "key": self.key,
            "fields": {
                "description": self.description,
                "updated": self.updated,
                "attachment": self.attachments,
            },
        }

    def update_issue(self, key: str, fields: dict[str, Any] | None = None, **_: Any) -> None:
        self.puts += 1
        written = (fields or {}).get("description", "")
        self.description = self.on_write(written) if self.on_write else written
        self.updated = "2026-07-29T11:00:00.000+0900"


@pytest.fixture
def pulled(tmp_path: Path) -> tuple[FakeIssue, Path]:
    issue = FakeIssue()
    path = tmp_path / "description.wiki"
    description_wiki.pull_wiki(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    return issue, path


# --------------------------------------------------------------------------
# What was read is what is on disk
# --------------------------------------------------------------------------


def test_the_file_is_the_description_byte_for_byte(pulled: tuple[FakeIssue, Path]) -> None:
    """The whole claim of this path. Any transformation here would make it the
    thing it exists to be an alternative to."""

    issue, path = pulled
    assert path.read_bytes() == issue.description.encode("utf-8")


def test_the_binding_records_both_the_id_and_the_key(pulled: tuple[FakeIssue, Path]) -> None:
    """A key can be moved to another project while the id stays put. On the key
    alone a file follows a rename onto an issue it was never pulled from; on the
    id alone a person cannot tell which issue a stray file belongs to."""

    _issue, path = pulled
    binding = read_binding(path)
    assert binding is not None
    assert binding.issue_id == "10001"
    assert binding.issue_key == "DEMO600-1"


def test_the_hash_is_over_the_description_not_the_envelope(pulled: tuple[FakeIssue, Path]) -> None:
    """The HTTP JSON carries fields that move without the description moving. A
    binding that changes when nothing changed teaches its reader to ignore it."""

    from atlassian_skills.jira.description_binding import source_sha256

    issue, path = pulled
    binding = read_binding(path)
    assert binding is not None
    assert binding.source_sha256 == source_sha256(issue.description)


def test_an_empty_description_is_readable_rather_than_an_error(tmp_path: Path) -> None:
    """Jira returns null for an issue with no description. A workflow that
    cannot open those cannot be the escape hatch for anything."""

    issue = FakeIssue(description="")
    issue.description = None  # type: ignore[assignment]
    path = tmp_path / "d.wiki"
    result = description_wiki.pull_wiki(issue, "DEMO600-1", output_path=path)
    assert result["bytes"] == 0
    assert path.read_bytes() == b""


# --------------------------------------------------------------------------
# Noticing that the issue moved
# --------------------------------------------------------------------------


def test_a_description_that_changed_is_refused_even_when_updated_did_not(
    pulled: tuple[FakeIssue, Path],
) -> None:
    """The case that separates a real stale check from a plausible one.

    Binding on `updated` alone passes here: the timestamp is untouched. The
    description is not, and publishing over it would discard somebody's edit
    with nothing to show that it happened.
    """

    issue, path = pulled
    path.write_bytes(b"my edit\n")
    issue.description = "somebody else edited this"  # `updated` deliberately unchanged

    with pytest.raises(ConflictError) as caught:
        description_wiki.push_wiki(issue, "DEMO600-1", path)

    assert caught.value.context["reason"] == "description_remote_changed"
    assert issue.puts == 0


def test_a_file_bound_to_a_different_issue_is_refused(pulled: tuple[FakeIssue, Path]) -> None:
    """Same key, different issue behind it. Without the id this reads as an
    ordinary stale conflict and the advice would be to merge -- into an issue
    the file was never pulled from."""

    issue, path = pulled
    path.write_bytes(b"my edit\n")
    issue.issue_id = "99999"

    with pytest.raises(ValidationError) as caught:
        description_wiki.push_wiki(issue, "DEMO600-1", path)

    assert caught.value.context["reason"] == "description_binding_issue_mismatch"
    assert issue.puts == 0


def test_a_file_with_no_binding_is_refused_unless_asked_for(tmp_path: Path) -> None:
    """A push with no binding cannot tell whether the issue changed. That is a
    thing a caller may choose, and not a thing that should happen by default."""

    issue = FakeIssue()
    path = tmp_path / "loose.wiki"
    path.write_bytes(b"written by hand\n")

    with pytest.raises(ValidationError) as caught:
        description_wiki.push_wiki(issue, "DEMO600-1", path)
    assert caught.value.context["reason"] == "description_binding_missing"
    assert issue.puts == 0

    result = description_wiki.push_wiki(issue, "DEMO600-1", path, allow_unbound=True)
    assert result["status"] == "updated"
    assert issue.puts == 1


# --------------------------------------------------------------------------
# Writing, and checking what landed
# --------------------------------------------------------------------------


def test_an_unchanged_file_writes_nothing(pulled: tuple[FakeIssue, Path]) -> None:
    """Republishing an untouched file must not bump `updated` and must not
    appear in anybody's activity feed."""

    issue, path = pulled
    result = description_wiki.push_wiki(issue, "DEMO600-1", path)
    assert result["status"] == "no_change"
    assert issue.puts == 0


def test_a_dry_run_reports_the_change_and_writes_nothing(pulled: tuple[FakeIssue, Path]) -> None:
    issue, path = pulled
    path.write_bytes(b"h2. Title\n\n* one\n* changed\n")

    result = description_wiki.push_wiki(issue, "DEMO600-1", path, dry_run=True)

    assert result["status"] == "dry_run"
    assert result["first_difference"]["line"] == 4
    assert issue.puts == 0


def test_a_server_rewrite_is_reported_rather_than_assumed_away(pulled: tuple[FakeIssue, Path]) -> None:
    """Jira has stored description bytes verbatim in every measurement so far,
    and "so far" is not "always". A caller republishing a body the server
    rewrote would keep undoing the rewrite, once per edit, silently."""

    issue, path = pulled
    issue.on_write = lambda written: written.replace("changed", "normalised")
    path.write_bytes(b"h2. Title\n\n* one\n* changed\n")

    result = description_wiki.push_wiki(issue, "DEMO600-1", path)

    assert result["status"] == "updated"
    assert result["description_matches_sent"] is False
    assert result["first_difference"]["line"] == 4


def test_the_binding_is_rebound_to_what_the_server_kept(pulled: tuple[FakeIssue, Path]) -> None:
    """Rebound to the readback, not to what was sent. Binding to the sent text
    after a rewrite would make the very next push report a phantom conflict."""

    issue, path = pulled
    issue.on_write = lambda written: written + "\n"
    path.write_bytes(b"edited\n")

    description_wiki.push_wiki(issue, "DEMO600-1", path)

    binding = read_binding(path)
    assert binding is not None
    assert binding.base_wiki == issue.description
    assert binding.remote_updated == issue.updated


# --------------------------------------------------------------------------
# What the file can still do
# --------------------------------------------------------------------------


def test_validate_says_which_capability_a_missing_binding_costs(tmp_path: Path) -> None:
    """Not "invalid". The file is still publishable; what it lost is the stale
    check and the merge, and naming which is the actionable part."""

    path = tmp_path / "loose.wiki"
    path.write_bytes(b"text\n")

    report = description_wiki.validate_wiki(path)

    assert report["status"] == "unbound"
    assert report["can_push"] is True
    assert report["can_push_with_stale_guard"] is False
    assert report["can_merge"] is False


def test_validate_notices_a_local_edit_without_the_network(pulled: tuple[FakeIssue, Path]) -> None:
    _issue, path = pulled
    assert description_wiki.validate_wiki(path)["edited"] is False
    path.write_bytes(b"edited\n")
    assert description_wiki.validate_wiki(path)["edited"] is True


def test_diff_separates_a_local_edit_from_a_remote_one(pulled: tuple[FakeIssue, Path]) -> None:
    """A diff that shows only local edits hides the case that matters most --
    somebody else changed the description while this file sat on disk."""

    issue, path = pulled
    path.write_bytes(b"my edit\n")
    issue.description = "their edit\n"

    report = description_wiki.diff_wiki(issue, "DEMO600-1", path)

    assert report["local_edited"] is True
    assert report["remote_changed_since_pull"] is True
    assert report["identical"] is False


def test_the_binding_file_sits_beside_the_document(pulled: tuple[FakeIssue, Path]) -> None:
    """Not a dotfile. A hidden file is one a person copies without noticing, and
    then wonders why the stale check stopped working."""

    _issue, path = pulled
    assert binding_path(path).exists()
    assert not binding_path(path).name.startswith(".")
    assert json.loads(binding_path(path).read_text(encoding="utf-8"))["schema"] == "atls-jira-description-v1"


# --------------------------------------------------------------------------
# The exact path must not publish a file that is not exact
# --------------------------------------------------------------------------


def test_a_markdown_managed_file_cannot_be_published_as_exact_wiki(tmp_path: Path) -> None:
    """The reverse of the check the Markdown path makes, and it was missing.

    This path writes the file's bytes to Jira verbatim, which is right for wiki
    and catastrophic for anything else. Reproduced before the fix: a Markdown
    file published `# Markdown Title` and `- bullet` as the description, and
    Jira spells those `h1.` and `*` -- so the issue rendered the SOURCE of a
    document rather than the document.
    """

    from atlassian_skills.jira import description_md

    issue = FakeIssue()
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path)  # authority = md
    path.write_bytes(b"# Markdown Title\n\n- bullet\n")

    with pytest.raises(ValidationError) as caught:
        description_wiki.push_wiki(issue, "DEMO600-1", path)

    assert caught.value.context["reason"] == "markdown_is_authoritative"
    assert issue.puts == 0


def test_the_wiki_path_still_publishes_a_wiki_managed_file(pulled: tuple[FakeIssue, Path]) -> None:
    """The guard has to have a bottom, or the workflow it protects cannot run."""

    issue, path = pulled
    path.write_bytes(b"h2. Title\n\nedited\n")

    result = description_wiki.push_wiki(issue, "DEMO600-1", path)

    assert result["status"] == "updated"
    assert issue.puts == 1
