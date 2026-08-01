"""Publishing managed Markdown: what must be true before a PUT, and in what order.

Two questions that look like one and are not:

    is this candidate safe against the body it was derived from?
    is that body still what the issue holds?

A publish asking only the first overwrites a concurrent edit with something
perfectly well-formed. Both are tested here, and so is the ordering between
them -- the candidate is built once, before the fresh read, and the candidate
that was PROVEN is the candidate that is SENT. Rebuilding it after the checks
would mean proving one document and publishing another.

Not reachable from the CLI yet. Merge and recovery land before any of this is
exposed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from atlassian_skills.core.errors import ConflictError, ValidationError
from atlassian_skills.jira import description_md, description_push
from atlassian_skills.jira.description_binding import read_binding

PLAIN = "h2. Title\n\nplain paragraph\n"


class FakeIssue:
    base_url = "https://jira.example.com"

    def __init__(self, description: str = PLAIN) -> None:
        self.description = description
        self.issue_id = "10001"
        self.updated = "2026-07-29T10:00:00.000+0900"
        self.puts = 0
        self.sent: list[str] = []
        self.on_write: Any = None
        self.attachments: list[dict[str, str]] = []

    def get_issue_raw(self, key: str, fields: list[str] | None = None) -> dict[str, Any]:
        return {
            "id": self.issue_id,
            "key": "DEMO600-1",
            "fields": {"description": self.description, "updated": self.updated, "attachment": self.attachments},
        }

    def update_issue(self, key: str, fields: dict[str, Any] | None = None, **_: Any) -> None:
        self.puts += 1
        written = (fields or {}).get("description", "")
        self.sent.append(written)
        self.description = self.on_write(written) if self.on_write else written
        self.updated = "2026-07-29T11:00:00.000+0900"


@pytest.fixture
def managed(tmp_path: Path) -> tuple[FakeIssue, Path]:
    issue = FakeIssue()
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    return issue, path


# --------------------------------------------------------------------------
# Is the candidate safe against what it came from?
# --------------------------------------------------------------------------


def _rebind(path: Path, **changes: Any) -> None:
    """Rewrite the binding, so a test can stage a remote the fake cannot."""

    from atlassian_skills.jira.description_binding import DescriptionBinding, write_binding

    binding = read_binding(path)
    assert binding is not None
    write_binding(path, DescriptionBinding(**{**binding.__dict__, **changes}))


@pytest.mark.parametrize(
    ("base_wiki", "why"),
    [
        ("[~alice] and [~bob] review", "identity_values_lost"),
        ("[~carol] review", "identity_values_lost"),
        ("!diagram.png|thumbnail! see it", "identity_values_lost"),
    ],
    ids=["one-of-two-mentions", "different-mention", "attachment-reference"],
)
def test_identity_is_proven_by_value_and_count_not_by_kind(
    base_wiki: str, why: str, managed: tuple[FakeIssue, Path]
) -> None:
    """The defect this replaced: comparing the SET OF KINDS present.

    A candidate that keeps one of two mentions still "has mentions". One that
    swaps `[~alice]` for `[~bob]` still "has mentions". One that renames an
    attachment still "has an attachment reference". All three detach something
    from the issue, and a kind comparison calls all three unchanged.
    """

    issue, path = managed
    _rebind(path, base_wiki=base_wiki, base_markdown="")
    path.write_bytes(b"## Title\n\nno identity at all\n")

    with pytest.raises(ValidationError) as caught:
        description_push.push_md(issue, "DEMO600-1", path)

    assert caught.value.context["reason"] == "candidate_proof_failed"
    assert why in caught.value.context["findings"]
    assert issue.puts == 0


def test_converter_drift_is_refused_because_the_baseline_stopped_being_one(
    managed: tuple[FakeIssue, Path],
) -> None:
    """The check easiest to leave out and hardest to recover from.

    What publishes is the file's Markdown converted. If converting the file's
    OWN base no longer reproduces the wiki it was pulled from, the converter
    changed underneath it -- and every difference the proof measures is then
    measured against the wrong baseline, so a clean result means nothing.
    """

    issue, path = managed
    _rebind(path, base_markdown="## Title\n\nthis does not render to the stored wiki\n")
    path.write_bytes(b"## Title\n\nedited\n")

    with pytest.raises(ValidationError) as caught:
        description_push.push_md(issue, "DEMO600-1", path)

    assert "converter_drift" in caught.value.context["findings"]
    assert caught.value.context["base_round_trip_reproduces_source"] is False
    assert issue.puts == 0


def test_an_attachment_that_moved_since_the_pull_is_refused(tmp_path: Path) -> None:
    """Proven against the FRESH read, not the binding. An attachment that
    changed since the pull is only visible in a current read, and certifying
    from the binding alone would certify a state that is no longer there.

    Staged as the case that actually costs something: `diagram.png` is deleted
    and a different file is uploaded under the same name. Every reference in the
    body still reads identically and every one of them now resolves to another
    attachment, which is the one way this can go wrong without a single
    character of the description changing.
    """

    issue = FakeIssue("h2. Overview\n\n!diagram.png!\n")
    issue.attachments = [{"id": "42", "filename": "diagram.png", "created": "", "size": "10"}]
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)

    markdown = path.read_bytes().decode("utf-8")
    path.write_bytes(markdown.replace("## Overview", "## Overview and detail").encode("utf-8"))
    issue.attachments = [{"id": "77", "filename": "diagram.png", "created": "", "size": "1"}]

    with pytest.raises(ValidationError) as caught:
        description_push.push_md(issue, "DEMO600-1", path)

    assert "attachment_identity_changed" in caught.value.context["findings"]
    assert issue.puts == 0


def test_an_ordinary_edit_is_not_mistaken_for_an_identity_loss(managed: tuple[FakeIssue, Path]) -> None:
    """The check has to have a bottom or it refuses every edit and the workflow
    is a promise nobody can use."""

    issue, path = managed
    path.write_bytes(b"## Title\n\nrewritten paragraph\n")

    result = description_push.push_md(issue, "DEMO600-1", path)

    assert result["status"] == "updated"
    assert issue.puts == 1


def test_a_description_carrying_identity_pulls_publishes_and_is_proven_carrying_it(
    tmp_path: Path,
) -> None:
    """The case `markdown_identity_bound` exists for, end to end.

    A grade that nothing can satisfy is not a strict grade, it is a dead branch,
    and the refusal tests above would all still pass with one. So this walks the
    whole path -- pull, edit the prose, push -- on a body with a smart link and
    an attachment reference in it.

    The last two assertions are the ones that make it evidence. `safe is True`
    is also what an empty comparison returns, so the proof is asked what it
    actually LOOKED at: both kinds, present before and after. Without that, a
    proof that silently stopped finding identity would pass this test.
    """

    body = "h2. Overview\n\nSee [the design|https://example.test/d].\n\n!diagram.png!\n"
    issue = FakeIssue(body)
    issue.attachments = [{"id": "42", "filename": "diagram.png", "created": "", "size": "10"}]
    path = tmp_path / "description.md"

    pulled = description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    assert pulled["grade"]["status"] == "markdown_identity_bound"

    markdown = path.read_bytes().decode("utf-8")
    assert "[the design](https://example.test/d)" in markdown
    path.write_bytes(markdown.replace("## Overview", "## Overview and detail").encode("utf-8"))

    result = description_push.push_md(issue, "DEMO600-1", path)

    assert result["status"] == "updated"
    assert result["description_matches_sent"] is True
    proof = result["proof"]
    assert proof["safe"] is True
    assert proof["identity_lost"] == {}
    assert sorted(proof["identity_before"]) == ["attachment_reference", "smart_link"]
    assert proof["identity_after"] == proof["identity_before"]
    assert "[the design|https://example.test/d]" in issue.description


def test_an_ambiguous_reference_is_refused_even_when_no_attachment_moved(tmp_path: Path) -> None:
    """The case that makes this check its own check rather than decoration.

    Upload a second `diagram.png` between the pull and the push and the SET
    changed, so `attachment_identity_changed` refuses it and this finding never
    has to. What is staged here instead is a binding that already knew about
    both -- written before this rule existed, or by hand. Nothing moved, every
    other question answers cleanly, and the reference still does not resolve to
    one attachment.
    """

    duplicates = [
        {"id": "42", "filename": "diagram.png", "created": "", "size": "10"},
        {"id": "77", "filename": "diagram.png", "created": "", "size": "11"},
    ]
    issue = FakeIssue("h2. Overview\n\n!diagram.png!\n")
    issue.attachments = [duplicates[0]]
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)

    # Both sides see both attachments, so the set is unchanged and only the
    # unresolvable reference is left to object to.
    issue.attachments = list(duplicates)
    _rebind(path, attachments=tuple(duplicates))
    path.write_bytes(b'## Overview and detail\n\n![](diagram.png)<!-- cfxmark:asset src="diagram.png" -->\n')

    with pytest.raises(ValidationError) as caught:
        description_push.push_md(issue, "DEMO600-1", path)

    assert caught.value.context["findings"] == ["attachment_filename_ambiguous"]
    assert caught.value.context["ambiguous_attachment_references"] == ["diagram.png"]
    assert issue.puts == 0


def test_a_carried_body_that_drops_its_link_is_still_refused(tmp_path: Path) -> None:
    """The other half of the same claim. Admitting these bodies is only safe
    because the publish checks them, so the check is shown refusing one."""

    body = "h2. Overview\n\nSee [the design|https://example.test/d].\n\n!diagram.png!\n"
    issue = FakeIssue(body)
    issue.attachments = [{"id": "42", "filename": "diagram.png", "created": "", "size": "10"}]
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)

    path.write_bytes(b"## Overview\n\nSee the design.\n\n![](diagram.png)\n")

    with pytest.raises(ValidationError) as caught:
        description_push.push_md(issue, "DEMO600-1", path)

    assert caught.value.context["reason"] == "candidate_proof_failed"
    assert caught.value.context["identity_lost"]["smart_link"] == ["[the design|https://example.test/d]"]
    assert issue.puts == 0


# --------------------------------------------------------------------------
# Publishing may move the stored bytes by whitespace. It may not do it quietly.
#
# The grade is decided on a NORMALISED round trip -- `read_projection.comparable`
# drops blank lines and trailing spaces -- because comparing them called every
# faithful body a change. The publish compares bytes. So a body admitted as
# manageable can still differ from what publishing it would store, by exactly
# that much, and both halves are right about their own question.
#
# Before this was named, the exact comparison called it `converter_drift`: an
# ordinary description with a space at the end of a line became unpublishable,
# and the reason given was a converter defect that does not exist.
# --------------------------------------------------------------------------

#: A paragraph ending in a space. Nothing exotic -- editors leave these behind.
TRAILING_SPACE = "h2. Title\n\nsome prose. \n"


def test_a_trailing_space_is_published_and_named_rather_than_refused(tmp_path: Path) -> None:
    issue = FakeIssue(TRAILING_SPACE)
    path = tmp_path / "description.md"

    pulled = description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    assert pulled["grade"]["status"] == "markdown_ready"

    result = description_push.push_md(issue, "DEMO600-1", path)

    assert result["status"] == "updated"
    assert result["change_class"] == "whitespace_only_change"
    assert "converter_drift" not in result["proof"]["findings"]
    assert issue.description == "h2. Title\n\nsome prose.\n"


def test_the_same_body_a_second_time_changes_nothing(tmp_path: Path) -> None:
    """The whitespace move happens once. Without this, `whitespace_only_change`
    could be describing a body that shifts on every push, which is a different
    and much worse thing -- and the fixed-point test elsewhere would be the only
    place it showed up."""

    issue = FakeIssue(TRAILING_SPACE)
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    description_push.push_md(issue, "DEMO600-1", path)

    again = description_push.push_md(issue, "DEMO600-1", path)

    assert again["status"] == "no_change"
    assert again["change_class"] == "no_change"
    assert issue.puts == 1


def test_an_edit_that_changes_words_is_classified_as_content(managed: tuple[FakeIssue, Path]) -> None:
    """The bottom of the classification. Without it, code that answers
    `whitespace_only_change` to everything would satisfy the tests above."""

    issue, path = managed
    path.write_bytes(b"## Title\n\nsomething a reader would notice\n")

    result = description_push.push_md(issue, "DEMO600-1", path)

    assert result["change_class"] == "content_change"


def test_a_dry_run_says_the_change_would_be_whitespace_before_it_happens(tmp_path: Path) -> None:
    """A caller running a dry run to decide whether to publish is exactly the
    caller who needs to know the only difference is whitespace."""

    issue = FakeIssue(TRAILING_SPACE)
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)

    result = description_push.push_md(issue, "DEMO600-1", path, dry_run=True)

    assert result["status"] == "dry_run"
    assert result["change_class"] == "whitespace_only_change"
    assert issue.puts == 0


def test_diff_says_it_too_because_identical_markdown_is_a_different_question(
    tmp_path: Path,
) -> None:
    """`identical` compares two Markdowns, and they can agree while the bytes
    Jira holds still move. A caller reading only that is told nothing changes
    and then watches the description's whitespace change."""

    issue = FakeIssue(TRAILING_SPACE)
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)

    report = description_md.diff_md(issue, "DEMO600-1", path)

    assert report["identical"] is True
    assert report["change_class"] == "whitespace_only_change"


# --------------------------------------------------------------------------
# Is that body still what the issue holds?
# --------------------------------------------------------------------------


def test_a_remote_edit_is_refused_even_though_the_candidate_is_fine(
    managed: tuple[FakeIssue, Path],
) -> None:
    """The second question. This candidate is safe against the body it was
    derived from, and that body is no longer what the issue says."""

    issue, path = managed
    path.write_bytes(b"## Title\n\nmy edit\n")
    issue.description = "h2. Title\n\nsomebody else got here first\n"

    with pytest.raises(ConflictError) as caught:
        description_push.push_md(issue, "DEMO600-1", path)

    assert caught.value.context["reason"] == "description_remote_changed"
    assert issue.puts == 0


def test_a_file_bound_to_another_issue_is_refused(managed: tuple[FakeIssue, Path]) -> None:
    issue, path = managed
    path.write_bytes(b"## Title\n\nedited\n")
    issue.issue_id = "99999"

    with pytest.raises(ValidationError) as caught:
        description_push.push_md(issue, "DEMO600-1", path)

    assert caught.value.context["reason"] == "description_binding_issue_mismatch"
    assert issue.puts == 0


def test_markdown_cannot_publish_where_the_wiki_side_is_authoritative(
    managed: tuple[FakeIssue, Path],
) -> None:
    """Two representations publishing from one directory race each other, and
    the loser's edit disappears without either workflow noticing."""

    issue, path = managed
    binding = read_binding(path)
    assert binding is not None
    from atlassian_skills.jira.description_binding import DescriptionBinding, write_binding

    write_binding(path, DescriptionBinding(**{**binding.__dict__, "authority": "wiki"}))

    with pytest.raises(ValidationError) as caught:
        description_push.push_md(issue, "DEMO600-1", path)

    assert caught.value.context["reason"] == "wiki_is_authoritative"
    assert issue.puts == 0


# --------------------------------------------------------------------------
# What is proven is what is sent
# --------------------------------------------------------------------------


def test_the_candidate_proven_is_the_candidate_sent(managed: tuple[FakeIssue, Path]) -> None:
    """Rebuilding after the checks would mean proving one document and
    publishing another, which is the exact shape of defect a corpus was spent
    finding elsewhere in this project."""

    issue, path = managed
    path.write_bytes(b"## Title\n\nrewritten\n")

    candidate = description_push.build_candidate(path.read_bytes().decode("utf-8"))
    description_push.push_md(issue, "DEMO600-1", path)

    assert issue.sent == [candidate]


def test_an_unchanged_file_writes_nothing(managed: tuple[FakeIssue, Path]) -> None:
    issue, path = managed
    result = description_push.push_md(issue, "DEMO600-1", path)
    assert result["status"] == "no_change"
    assert issue.puts == 0


def test_a_dry_run_proves_everything_and_writes_nothing(managed: tuple[FakeIssue, Path]) -> None:
    issue, path = managed
    path.write_bytes(b"## Title\n\nrewritten\n")

    result = description_push.push_md(issue, "DEMO600-1", path, dry_run=True)

    assert result["status"] == "dry_run"
    assert result["proof"]["safe"] is True
    assert issue.puts == 0


def test_a_server_rewrite_is_reported_and_the_binding_follows_it(
    managed: tuple[FakeIssue, Path],
) -> None:
    """Rebound to what the server KEPT. Binding to what was sent would make the
    very next push report a conflict nobody caused."""

    issue, path = managed
    issue.on_write = lambda written: written + "\n"
    path.write_bytes(b"## Title\n\nrewritten\n")

    result = description_push.push_md(issue, "DEMO600-1", path)

    assert result["description_matches_sent"] is False
    binding = read_binding(path)
    assert binding is not None
    assert binding.base_wiki == issue.description


def test_markdown_that_cannot_be_rendered_is_refused_before_anything_else(
    managed: tuple[FakeIssue, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body we cannot render is one we cannot publish, and finding that out
    after the fresh read would have spent a request to learn it."""

    issue, path = managed

    def explode(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("converter says no")

    monkeypatch.setattr("atlassian_skills.jira.description_push.cfxmark.to_jira_wiki", explode)
    path.write_bytes(b"## Title\n\nrewritten\n")

    with pytest.raises(ValidationError) as caught:
        description_push.push_md(issue, "DEMO600-1", path)

    assert caught.value.context["reason"] == "candidate_conversion_failed"
    assert issue.puts == 0


# --------------------------------------------------------------------------
# Guards that were advertised and were not there, and one that was too wide
# --------------------------------------------------------------------------


def test_a_reference_to_a_file_the_issue_does_not_have_is_refused(tmp_path: Path) -> None:
    """`SKILL.md` and the CHANGELOG both say adding a reference is refused.

    Only removal and renaming were ever checked. Identity compares
    `Counter(before) - Counter(after)`, which is empty whenever the candidate
    keeps everything the base had -- including when it keeps all of it and adds
    one more. So a reference to a filename no attachment carries published, and
    the issue then renders a broken image while the caller was told `updated`.
    """

    issue = FakeIssue("h2. Overview\n\n!diagram.png!\n")
    issue.attachments = [{"id": "42", "filename": "diagram.png", "created": "", "size": "10"}]
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)

    markdown = path.read_bytes().decode("utf-8")
    path.write_bytes((markdown + "\n![](missing.png)\n").encode("utf-8"))

    with pytest.raises(ValidationError) as caught:
        description_push.push_md(issue, "DEMO600-1", path)

    assert "attachment_reference_unresolved" in caught.value.context["findings"]
    assert caught.value.context["unresolved_attachment_references"] == ["missing.png"]
    assert issue.puts == 0


def test_an_attachment_the_body_never_mentions_does_not_block_the_push(
    managed: tuple[FakeIssue, Path],
) -> None:
    """Attaching a file to an issue is an ordinary thing to do.

    The identity check compared the WHOLE attachment set, so a log somebody
    dropped on the issue while a description was being edited refused the push
    -- with a finding about attachments, on a body that references none. The
    question the check exists to ask is whether the references in this body
    still mean what they meant, and an attachment nothing references cannot
    change that answer.
    """

    issue, path = managed
    path.write_bytes(b"## Title\n\nedited\n")
    issue.attachments = [{"id": "77", "filename": "somebody-elses-log.txt", "created": "", "size": "1"}]

    result = description_push.push_md(issue, "DEMO600-1", path)

    assert result["status"] == "updated"
    assert issue.puts == 1


def test_a_readback_holding_somebody_elses_body_is_not_reported_as_updated(
    managed: tuple[FakeIssue, Path],
) -> None:
    """Exit 0 and `status: updated` while the issue holds another person's text.

    The readback was compared and the answer was carried in
    `description_matches_sent`, which nothing had to look at. A caller reading
    the status sees a successful publish; the issue has somebody else's
    description in it. The journal was cleared too, so the next run had nothing
    left to reconcile from.
    """

    from atlassian_skills.jira.description_journal import journal_path

    issue, path = managed
    issue.on_write = lambda _written: "h2. Somebody else\n\ntheir words entirely\n"
    path.write_bytes(b"## Title\n\nedited\n")

    with pytest.raises(ConflictError) as caught:
        description_push.push_md(issue, "DEMO600-1", path)

    assert caught.value.context["reason"] == "description_readback_mismatch"
    assert journal_path(path).exists()


def test_a_journal_with_a_field_this_version_does_not_know_is_refused_not_crashed(
    managed: tuple[FakeIssue, Path],
) -> None:
    """A journal written by a newer version reached `DescriptionOperation(**known)`.

    Unknown keyword arguments raise `TypeError`, which the caller did not catch,
    so the traceback went to stderr and a `--format=json` consumer got empty
    stdout. `read_binding` already treats the same situation as a thing to
    decide about rather than a crash.
    """

    import json

    from atlassian_skills.jira.description_journal import SCHEMA, journal_path

    issue, path = managed
    journal_path(path).write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "operation_id": "op_0",
                "issue_id": "10001",
                "issue_key": "DEMO600-1",
                "authority": "md",
                "base_sha256": "sha256:" + "0" * 64,
                "candidate_sha256": "sha256:" + "1" * 64,
                "attachments_sha256": "sha256:" + "2" * 64,
                "stage": "planned",
                "proof_bundle": "sha256:" + "3" * 64,
                "settled_by": "a field this version has never heard of",
            }
        ),
        encoding="utf-8",
    )
    path.write_bytes(b"## Title\n\nedited\n")

    with pytest.raises(ValidationError) as caught:
        description_push.push_md(issue, "DEMO600-1", path)

    assert caught.value.context["reason"] == "description_operation_unreadable"
    assert issue.puts == 0


# --------------------------------------------------------------------------
# D1: the window a client cannot close, said out loud
# --------------------------------------------------------------------------


def test_the_dry_run_names_the_write_window_jira_cannot_close(tmp_path: Path) -> None:
    """Accepted is not the same as hidden.

    Jira Server/DC's issue update endpoint has no precondition — no `If-Match`, no
    version field — so between the fresh read and the server applying the PUT there is an
    interval no client-side check can close. A save landing there is overwritten and the
    receipt says `updated`. `DECISIONS.md` D-4 accepts that; R3-2 is where it was found.

    What D-4 does not license is silence. A caller deciding whether to publish is entitled
    to know the guarantee is best-effort rather than conditional, and to know it *before*
    the write rather than from a post-mortem — which is the same argument the
    whitespace-only class above already won.
    """

    issue = FakeIssue(TRAILING_SPACE)
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)

    result = description_push.push_md(issue, "DEMO600-1", path, dry_run=True)

    concurrency = result["concurrency"]
    assert concurrency["guarantee"] == "best_effort"
    # The reason, not just the label: an agent that knows *why* cannot mistake it for a
    # transient failure to retry around.
    assert concurrency["server_conditional_write"] is False
    assert "precondition" in concurrency["detail"]
    assert issue.puts == 0


def test_a_real_push_says_it_too_because_the_receipt_is_what_gets_kept(tmp_path: Path) -> None:
    """The dry run is advice; the receipt is the record.

    A receipt that says `updated` with nothing beside it reads as a conditional write that
    succeeded. It was not conditional, and six months later the receipt is all anyone has.
    """

    issue = FakeIssue(TRAILING_SPACE)
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    path.write_text(path.read_text(encoding="utf-8") + "\nAdded.\n", encoding="utf-8")

    result = description_push.push_md(issue, "DEMO600-1", path)

    assert issue.puts == 1
    assert result["concurrency"]["guarantee"] == "best_effort"
    assert result["concurrency"]["server_conditional_write"] is False
