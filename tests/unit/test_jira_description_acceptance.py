"""The managed description workflow, judged as a whole rather than per function.

Everything here is a property of the round trip end to end. The unit files each
pin one decision; this one asks whether the decisions add up to a workflow
somebody can use without losing text.

**What "fixed point" means here, precisely.** A first push may move the stored
bytes by whitespace, because the grade is decided on a normalised round trip
(`read_projection.comparable` drops blank lines and trailing spaces) and the
publish compares bytes. That is allowed and it is classified. What is NOT
allowed is for it to keep happening: from the second cycle on, the same file
must produce `no_change` and the stored bytes must not move again. A workflow
that shifts whitespace every time would pass a single-push test and rewrite the
issue on every run forever.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from atlassian_skills.core.errors import ConflictError, ValidationError
from atlassian_skills.jira import description_md, description_merge, description_push, description_wiki

#: One shape per thing that could break the cycle: a heading, a list, a table,
#: an attachment reference, a smart link, and a line ending in a space.
CYCLE_BODIES = {
    "plain": "h2. Title\n\nplain paragraph\n",
    "list": "h2. Title\n\n* one\n* two\n",
    "table": "h2. Title\n\n||a||b||\n|1|2|\n",
    "identity": "h2. Overview\n\nSee [the design|https://example.test/d].\n\n!diagram.png!\n",
    "trailing_space": "h2. Title\n\nsome prose. \n",
}


class FakeIssue:
    base_url = "https://jira.example.com"

    def __init__(self, description: str) -> None:
        self.description = description
        self.issue_id = "10001"
        self.updated = "2026-07-29T10:00:00.000+0900"
        self.puts = 0
        self.sent: list[str] = []
        self.attachments: list[dict[str, str]] = []
        self.on_write: Any = None

    def get_issue_raw(self, key: str, fields: list[str] | None = None) -> dict[str, Any]:
        return {
            "id": self.issue_id,
            "key": "DEMO600-1",
            "fields": {
                "description": self.description,
                "updated": self.updated,
                "attachment": list(self.attachments),
            },
        }

    def update_issue(self, key: str, fields: dict[str, Any] | None = None, **_: Any) -> None:
        self.puts += 1
        written = (fields or {}).get("description", "")
        self.sent.append(written)
        self.description = self.on_write(written) if self.on_write else written
        self.updated = f"2026-07-29T11:{self.puts:02d}:00.000+0900"


# --------------------------------------------------------------------------
# The cycle settles
# --------------------------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(CYCLE_BODIES))
def test_five_cycles_reach_a_fixed_point(shape: str, tmp_path: Path) -> None:
    """wiki -> md -> wiki, five times, on an unedited file.

    Two separate claims, and collapsing them would hide the interesting one:

        cycle 1     may move the stored bytes, and says so if it does
        cycles 2-5  change nothing at all
    """

    issue = FakeIssue(CYCLE_BODIES[shape])
    if shape == "identity":
        issue.attachments = [{"id": "42", "filename": "diagram.png", "created": "", "size": "10"}]
    path = tmp_path / "description.md"

    classes: list[str] = []
    stored: list[str] = []
    for _cycle in range(5):
        # Re-pulling each cycle is what makes this a round trip rather than five
        # pushes of one conversion.
        description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
        classes.append(description_push.push_md(issue, "DEMO600-1", path)["change_class"])
        stored.append(issue.description)

    assert classes[0] in {"no_change", "whitespace_only_change"}
    assert classes[1:] == ["no_change"] * 4, f"{shape} never settles: {classes}"
    assert stored[1:] == [stored[0]] * 4
    assert issue.puts <= 1


def test_the_cycle_keeps_the_identity_it_started_with(tmp_path: Path) -> None:
    """A fixed point is not enough on its own: a body that lost its link on the
    first cycle and then stayed lost would satisfy every assertion above."""

    issue = FakeIssue(CYCLE_BODIES["identity"])
    issue.attachments = [{"id": "42", "filename": "diagram.png", "created": "", "size": "10"}]
    path = tmp_path / "description.md"

    for _cycle in range(5):
        description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
        description_push.push_md(issue, "DEMO600-1", path)

    assert "[the design|https://example.test/d]" in issue.description
    assert "!diagram.png!" in issue.description


# --------------------------------------------------------------------------
# What is proven is what is sent is what came back
# --------------------------------------------------------------------------


def test_proven_sent_and_stored_are_one_document(tmp_path: Path) -> None:
    issue = FakeIssue(CYCLE_BODIES["plain"])
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    path.write_bytes(b"## Title\n\nan edit\n")

    proven = description_push.build_candidate(path.read_bytes().decode("utf-8"))
    result = description_push.push_md(issue, "DEMO600-1", path)

    assert issue.sent == [proven]
    assert issue.description == proven
    assert result["description_matches_sent"] is True


def test_a_server_that_keeps_something_else_is_refused_not_reported_as_updated(tmp_path: Path) -> None:
    """The one leg that can come apart without anybody doing anything wrong.

    It used to come back as `status: updated` at exit 0, with the answer in a
    `description_matches_sent` field nothing had to read. Whoever wrote the
    extra line is not this run, and a caller told the publish succeeded has no
    reason to go and look.
    """

    from atlassian_skills.core.errors import ConflictError
    from atlassian_skills.jira.description_journal import journal_path

    issue = FakeIssue(CYCLE_BODIES["plain"])
    issue.on_write = lambda written: written + "\ntrailing addition\n"
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    path.write_bytes(b"## Title\n\nan edit\n")

    with pytest.raises(ConflictError) as caught:
        description_push.push_md(issue, "DEMO600-1", path)

    assert caught.value.context["reason"] == "description_readback_mismatch"
    assert issue.description != issue.sent[0]
    # Kept: it is the only record that this candidate was sent, and the next run
    # needs it to tell a collision from a fresh local edit.
    assert journal_path(path).exists()


def test_a_server_that_only_normalises_whitespace_still_publishes(tmp_path: Path) -> None:
    """The other side of the same question, and the reason it is not one rule.

    Refusing every byte difference would refuse a server that appends a newline,
    which is a thing servers do and which changes nothing a reader sees. So the
    readback is classified the same way the change itself is: same words is a
    success that says so, different words is not a success at all.
    """

    issue = FakeIssue(CYCLE_BODIES["plain"])
    issue.on_write = lambda written: written + "\n"
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    path.write_bytes(b"## Title\n\nan edit\n")

    result = description_push.push_md(issue, "DEMO600-1", path)

    assert result["status"] == "updated"
    assert result["readback_class"] == "whitespace_only_change"
    assert result["description_matches_sent"] is False


# --------------------------------------------------------------------------
# Unsupported input writes nothing
# --------------------------------------------------------------------------


UNSUPPORTED = {
    "mention": "h2. Title\n\n[~alice] please review\n",
    "markdown_looking": "## 방향\n# first\n# second\n",
}


@pytest.mark.parametrize("shape", sorted(UNSUPPORTED))
def test_an_unsupported_body_is_never_pulled_and_never_written(shape: str, tmp_path: Path) -> None:
    issue = FakeIssue(UNSUPPORTED[shape])
    path = tmp_path / "description.md"

    with pytest.raises(ValidationError) as caught:
        description_md.pull_md(issue, "DEMO600-1", output_path=path)

    assert not path.exists()
    assert issue.puts == 0
    action = caught.value.context["next_actions"][0]
    assert action["argv"][:5] == ["jira", "issue", "description", "wiki", "pull"]
    # A complete argv, not a template. Angle brackets are the shape of a hole
    # left for a human, and a caller who has to fill one composes their own
    # command instead of running the one they were given.
    assert not any("<" in part for part in action["argv"])


def test_a_markdown_file_cannot_be_published_where_the_wiki_side_is_authoritative(
    tmp_path: Path,
) -> None:
    """Both representations publishing from one directory race each other, and
    the loser's edit disappears without either workflow noticing."""

    issue = FakeIssue(CYCLE_BODIES["plain"])
    path = tmp_path / "description.wiki"
    description_wiki.pull_wiki(issue, "DEMO600-1", output_path=path, site=issue.base_url)

    with pytest.raises(ValidationError) as caught:
        description_push.push_md(issue, "DEMO600-1", path)

    assert caught.value.context["reason"] == "wiki_is_authoritative"
    assert issue.puts == 0


def test_a_wiki_file_cannot_be_published_where_markdown_is_authoritative(tmp_path: Path) -> None:
    """The mirror of the case above, which is the one that was missing: a
    Markdown file's `# Title` published as exact wiki markup stores a heading
    Jira reads as a numbered list."""

    issue = FakeIssue(CYCLE_BODIES["plain"])
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)

    with pytest.raises(ValidationError) as caught:
        description_wiki.push_wiki(issue, "DEMO600-1", path)

    assert caught.value.context["reason"] == "markdown_is_authoritative"
    assert issue.puts == 0


# --------------------------------------------------------------------------
# Four ways to be blocked, four names
# --------------------------------------------------------------------------


def test_stale_merge_conflict_and_divergence_are_told_apart(tmp_path: Path) -> None:
    """One name for all of these would be true and useless.

    They need different people and different actions: a stale file needs a
    merge, a merge whose remote moved needs preparing again, and a diverged
    pending write needs somebody to read the issue history. A caller branching
    on `blocked` cannot pick between them.
    """

    reasons: list[str] = []

    # Stale: the description moved after the pull.
    issue = FakeIssue(CYCLE_BODIES["plain"])
    path = tmp_path / "stale.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    path.write_bytes(b"## Title\n\nmy edit\n")
    issue.description = "h2. Title\n\nsomebody else got here first\n"
    with pytest.raises(ConflictError) as stale:
        description_push.push_md(issue, "DEMO600-1", path)
    reasons.append(stale.value.context["reason"])
    assert issue.puts == 0

    # Merge conflict: the remote moved again while the merge was being read.
    description_merge.prepare_merge(issue, "DEMO600-1", path)
    merged = path.with_name(path.name + ".merge") / "merged.txt"
    merged.write_bytes(b"## Title\n\nboth edits, by hand\n")
    issue.description = "h2. Title\n\nand then a third person\n"
    with pytest.raises(ConflictError) as conflict:
        description_merge.finalize_merge(issue, "DEMO600-1", path, merged=merged)
    reasons.append(conflict.value.context["reason"])

    assert reasons == ["description_remote_changed", "remote_changed_since_prepare"]
    assert len(set(reasons)) == len(reasons)


def test_a_merge_that_settles_lets_the_push_through(tmp_path: Path) -> None:
    """The refusals above have to have a way out, or the workflow is a wall.

    Prepare, merge by hand, finalize, push -- and the push does not report the
    conflict the merge already settled.
    """

    issue = FakeIssue(CYCLE_BODIES["plain"])
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    path.write_bytes(b"## Title\n\nmy edit\n")
    issue.description = "h2. Title\n\ntheir edit\n"

    with pytest.raises(ConflictError):
        description_push.push_md(issue, "DEMO600-1", path)

    prepared = description_merge.prepare_merge(issue, "DEMO600-1", path)
    assert Path(prepared["base"]).exists()
    assert Path(prepared["remote"]).read_bytes().decode("utf-8") == issue.description

    merged = path.with_name(path.name + ".merge") / "merged.txt"
    merged.write_bytes(b"## Title\n\nmy edit and their edit\n")
    description_merge.finalize_merge(issue, "DEMO600-1", path, merged=merged)

    result = description_push.push_md(issue, "DEMO600-1", path)

    assert result["status"] == "updated"
    assert "my edit and their edit" in issue.description


# --------------------------------------------------------------------------
# P7's last completion condition: a remote edit landing mid-flight
# --------------------------------------------------------------------------


def test_a_remote_edit_between_pull_and_push_ends_in_put_0(tmp_path: Path) -> None:
    """§P7: a concurrent remote edit ends in PUT 0 or a named recovery, never a write.

    The Confluence half of this release found the same shape and it was a real defect
    there: `record` re-derived a fingerprint and then replaced the file with no lock, so
    two writers could both pass and both write. This is the Jira description equivalent of
    that window -- pull, somebody edits the issue in the browser, push -- and it had no
    test at all, which is why it is here rather than in the unit files.

    The refusal has to be named. A caller that cannot tell "somebody else edited this" from
    a transport error retries, and a retry is the one thing that must not happen while
    another person's text is on the issue.
    """

    issue = FakeIssue("h2. Title\n\noriginal paragraph\n")
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    path.write_text(path.read_text(encoding="utf-8").replace("original", "mine"), encoding="utf-8")

    # Somebody else, in the browser, after our pull and before our push.
    issue.description = "h2. Title\n\ntheirs, typed in the browser\n"
    issue.updated = "2026-07-29T10:30:00.000+0900"

    with pytest.raises((ConflictError, ValidationError)) as refused:
        description_push.push_md(issue, "DEMO600-1", path)

    assert issue.puts == 0, "the concurrent edit was overwritten"
    reason = (refused.value.context or {}).get("reason")
    # The measured vocabulary, and it is better than the one this test first demanded: it
    # names *what* changed rather than the class of problem, so a caller can tell a
    # concurrent description edit from a stale version number or a transport failure.
    assert reason == "description_remote_changed", reason
    # And their text is still there.
    assert "theirs, typed in the browser" in issue.description


def test_a_remote_edit_between_pull_and_push_leaves_our_file_alone(tmp_path: Path) -> None:
    """The local half of the same guarantee.

    A refusal that repaired the local file would take the edit away from the person who
    made it, which is the failure the Confluence side spent a review point on: only
    `record-reconciled-against` and `rebaseline` may touch a canonical body, and neither of
    them is a refused push.
    """

    issue = FakeIssue("h2. Title\n\noriginal paragraph\n")
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    path.write_text(path.read_text(encoding="utf-8").replace("original", "mine"), encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    issue.description = "h2. Title\n\ntheirs\n"
    issue.updated = "2026-07-29T10:30:00.000+0900"

    with pytest.raises((ConflictError, ValidationError)):
        description_push.push_md(issue, "DEMO600-1", path)

    assert path.read_text(encoding="utf-8") == before, "a refused push edited the local file"


def test_a_remote_edit_during_the_write_is_recoverable_rather_than_silent(tmp_path: Path) -> None:
    """The other half of the window: the edit arrives while our PUT is in flight.

    `on_write` makes the fake behave like a server that accepted our body and then had
    somebody else's edit land on top. The next run must not report that as success, and it
    must not re-PUT blindly -- the journal exists so this case has a name.
    """

    issue = FakeIssue("h2. Title\n\noriginal paragraph\n")
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    path.write_text(path.read_text(encoding="utf-8").replace("original", "mine"), encoding="utf-8")

    issue.on_write = lambda _written: "h2. Title\n\ntheirs, landed after ours\n"

    # Measured: this raises rather than returning a non-success receipt, and the message
    # says what is true -- Jira is not holding what we sent. Either shape satisfies §9.11
    # ("a readback that is not equivalent to the candidate is not a success"); what would
    # not is reporting `updated`.
    with pytest.raises((ConflictError, ValidationError)) as first:
        description_push.push_md(issue, "DEMO600-1", path)
    assert (first.value.context or {}).get("reason"), "the refusal has no name"
    puts_after_first = issue.puts

    # A second run is the recovery path, and it must not publish over their text.
    try:
        second = description_push.push_md(issue, "DEMO600-1", path)
        assert second["status"] != "updated", second
    except (ConflictError, ValidationError) as error:
        assert (error.context or {}).get("reason"), "the recovery refusal has no name"
    assert issue.puts == puts_after_first, "recovery published over the concurrent edit"
    assert "theirs, landed after ours" in issue.description


def test_an_attachment_rebound_to_a_different_upload_is_refused(tmp_path: Path) -> None:
    """P7 task 8, the direction with no named test: same filename, different attachment.

    Someone deletes `diagram.png` and uploads a new one. The reference in the body still
    reads `!diagram.png!` and now points at a different file, so a publish that reported
    success would silently rebind the reader to an image nobody chose.

    Identity is compared as `(id, filename)` pairs rather than filenames, which is what
    makes this visible at all -- a filename set would be identical before and after.
    """

    issue = FakeIssue("h2. Overview\n\n!diagram.png!\n")
    issue.attachments = [{"id": "42", "filename": "diagram.png", "created": "", "size": "10"}]
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    path.write_text(path.read_text(encoding="utf-8").replace("Overview", "Overview edited"), encoding="utf-8")

    # Same name, different upload.
    issue.attachments = [{"id": "99", "filename": "diagram.png", "created": "", "size": "11"}]

    with pytest.raises((ConflictError, ValidationError)) as refused:
        description_push.push_md(issue, "DEMO600-1", path)

    assert issue.puts == 0
    context = refused.value.context or {}
    findings = context.get("findings") or []
    assert "attachment_identity_changed" in findings or context.get("reason") == "attachment_identity_changed", context


def test_an_attachment_renamed_on_the_server_is_refused(tmp_path: Path) -> None:
    """The fourth direction: same upload, different filename.

    The body still references the old name, which no longer resolves. Left alone it
    publishes a broken image and reports success -- the failure the `unresolved` check was
    added for, reached by renaming rather than by adding.
    """

    issue = FakeIssue("h2. Overview\n\n!diagram.png!\n")
    issue.attachments = [{"id": "42", "filename": "diagram.png", "created": "", "size": "10"}]
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    path.write_text(path.read_text(encoding="utf-8").replace("Overview", "Overview edited"), encoding="utf-8")

    # Same id, renamed.
    issue.attachments = [{"id": "42", "filename": "architecture.png", "created": "", "size": "10"}]

    with pytest.raises((ConflictError, ValidationError)) as refused:
        description_push.push_md(issue, "DEMO600-1", path)

    assert issue.puts == 0
    context = refused.value.context or {}
    findings = context.get("findings") or []
    named = set(findings) | {context.get("reason")}
    assert named & {"attachment_identity_changed", "attachment_reference_unresolved"}, context


def test_a_save_landing_before_the_write_is_refused_with_put_0(tmp_path: Path) -> None:
    """R3-2, in the ordering my earlier tests did not reach.

    Those three replace the description *after* the fake accepts the candidate, so they exercise
    readback loss — the post-write window. The window review R3 found is earlier: between the fresh
    read and the PUT sat the whole prove step, and an unconditional write overwrote anything that
    landed there while reporting `updated`.

    This fake saves over the description on the read that happens immediately before the write, which
    is the ordering the reviewer reproduced. Jira Server/DC has no precondition on issue update, so
    the server cannot be asked to refuse; what closes this is reading once more with nothing between
    that read and the write, so everything a client can observe is caught at PUT 0.

    The residual is named rather than hidden: the interval between that response and the server
    applying the write is invisible to any client on an API without preconditions. That is a property
    of the endpoint, not a choice, and it is recorded in P7's evidence.
    """

    issue = FakeIssue("h2. Title\n\noriginal paragraph\n")
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    path.write_text(path.read_text(encoding="utf-8").replace("original", "mine"), encoding="utf-8")

    reads = {"n": 0}
    original_get = issue.get_issue_raw

    def get_issue_raw(key: str, fields: list[str] | None = None) -> dict[str, Any]:
        reads["n"] += 1
        # The second read of this push is the one immediately before the write. Saving here is the
        # concurrent edit arriving mid-push, after the candidate was proved against the first read.
        if reads["n"] == 2:
            issue.description = "h2. Title\n\ntheirs, saved first\n"
        return original_get(key, fields)

    issue.get_issue_raw = get_issue_raw  # type: ignore[method-assign]

    with pytest.raises((ConflictError, ValidationError)) as refused:
        description_push.push_md(issue, "DEMO600-1", path)

    assert issue.puts == 0, "the concurrent save was overwritten"
    context = refused.value.context or {}
    assert context.get("reason") == "description_remote_changed", context
    # Same remedy as the pre-push check, different moment. A caller counting refusals wants to know
    # the edit arrived mid-push rather than before the push started.
    assert context.get("detected") == "immediately_before_write", context
    assert "theirs, saved first" in issue.description


def test_the_exact_wiki_writer_discloses_the_same_write_window_as_the_markdown_one(tmp_path) -> None:
    """Two public writers, one disclosure. 0.4.0 makes both a contract.

    `description_push` re-reads immediately before the PUT and returns
    `concurrency.guarantee: best_effort`, because D-4 accepted that the interval between
    that response and the server applying the write cannot be closed by a client and so
    must be named. `push_wiki` re-reads at the top of the function but not again before
    writing, and returned no disclosure at all -- so the representation documented as
    "always available" was the one that said least about what it guarantees.
    """

    from atlassian_skills.jira.description_push import CONCURRENCY_DISCLOSURE
    from atlassian_skills.jira.description_wiki import pull_wiki, push_wiki

    client = FakeIssue("Original body")
    path = tmp_path / "d.wiki"
    pull_wiki(client, "PROJ-1", output_path=path)
    path.write_text("Edited body\n", encoding="utf-8")

    result = push_wiki(client, "PROJ-1", path)

    assert result["status"] in {"updated", "reconciled"}
    assert result["concurrency"] == CONCURRENCY_DISCLOSURE
