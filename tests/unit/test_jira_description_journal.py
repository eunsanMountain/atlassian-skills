"""Interrupt a description publish at every point it can be interrupted.

A PUT has nothing transactional under it, so each step here is somewhere a
process can stop existing. What the next run has to work from is a record of
what was going to be sent and a fresh read of what is there now.

The rule every test below is checking: **the journal records intent, the remote
decides the outcome.** A record saying "planned" cannot mean the write did not
land -- a lost response looks exactly like a request that never left -- so
nothing is concluded from the record alone. It exists to make two of the
remote's answers readable:

    remote holds the candidate   somebody put this here, and the record says it
                                 was us
    remote holds the base        nothing landed, unless the stage says the
                                 server acknowledged the write

Recovery is by re-running the same command. No test here repairs state by hand,
because a recovery that needs a person to edit a file is one that will be done
wrong at the moment it matters.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from atlassian_skills.core.errors import ConflictError, ValidationError
from atlassian_skills.jira import description_journal, description_md, description_push
from atlassian_skills.jira.description_binding import read_binding, source_sha256
from atlassian_skills.jira.description_journal import journal_path, read_journal

PLAIN = "h2. Title\n\nplain paragraph\n"
EDITED = b"## Title\n\nrewritten paragraph\n"


class Interrupted(RuntimeError):
    """A process that stopped existing, as far as this call can tell."""


class FakeIssue:
    """A Jira that can fail wherever a real one can."""

    base_url = "https://jira.example.com"

    def __init__(self, description: str = PLAIN) -> None:
        self.description = description
        self.issue_id = "10001"
        self.updated = "2026-07-29T10:00:00.000+0900"
        self.puts = 0
        self.sent: list[str] = []
        self.attachments: list[dict[str, str]] = []
        #: Raise instead of writing -- the request never reached Jira.
        self.refuse_write = False
        #: Write, then raise -- Jira took it and the answer was lost.
        self.lose_response = False
        #: Raise on the Nth read, counting from one. The readback is the read
        #: after a write, so this is how a readback is made to fail.
        self.fail_read_number: int | None = None
        self.reads = 0

    def get_issue_raw(self, key: str, fields: list[str] | None = None) -> dict[str, Any]:
        self.reads += 1
        if self.fail_read_number is not None and self.reads == self.fail_read_number:
            raise Interrupted("the read did not come back")
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
        if self.refuse_write:
            raise Interrupted("the request never left")
        self.puts += 1
        written = (fields or {}).get("description", "")
        self.sent.append(written)
        self.description = written
        self.updated = "2026-07-29T11:00:00.000+0900"
        if self.lose_response:
            raise Interrupted("the answer was lost")


@pytest.fixture
def managed(tmp_path: Path) -> tuple[FakeIssue, Path]:
    issue = FakeIssue()
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    return issue, path


def _push(issue: FakeIssue, path: Path) -> dict[str, Any]:
    return description_push.push_md(issue, "DEMO600-1", path)


# --------------------------------------------------------------------------
# 1-2. Before anything was sent
# --------------------------------------------------------------------------


def test_a_crash_before_the_record_leaves_nothing_to_recover(managed: tuple[FakeIssue, Path]) -> None:
    """Nothing was written and nothing was recorded, which is consistent.

    The next run is not a recovery at all -- it is the first attempt, and it has
    to behave exactly like one or the interrupted run has changed the meaning of
    a command that never ran.
    """

    issue, path = managed
    path.write_bytes(EDITED)
    issue.refuse_write = True

    with pytest.raises(Interrupted):
        _push(issue, path)

    assert issue.puts == 0
    assert issue.description == PLAIN

    issue.refuse_write = False
    result = _push(issue, path)

    assert result["status"] == "updated"
    assert issue.puts == 1
    assert not journal_path(path).exists()


def test_a_crash_after_the_record_and_before_the_put_sends_it_exactly_once(
    managed: tuple[FakeIssue, Path],
) -> None:
    """The record is on disk and the issue is untouched.

    The rerun re-proves everything from a fresh read rather than trusting the
    record, and then sends the write once. Sending it twice would be harmless
    here and is still wrong: it means the count of writes an operation makes
    depends on how many times it was interrupted.
    """

    issue, path = managed
    path.write_bytes(EDITED)
    issue.refuse_write = True

    with pytest.raises(Interrupted):
        _push(issue, path)

    pending = read_journal(path)
    assert pending is not None
    assert pending.stage == description_journal.PLANNED
    assert issue.description == PLAIN

    issue.refuse_write = False
    result = _push(issue, path)

    assert result["status"] == "updated"
    assert issue.puts == 1
    assert not journal_path(path).exists()


# --------------------------------------------------------------------------
# 3-5. Sent, and the run did not get to say so
# --------------------------------------------------------------------------


def test_a_lost_response_is_reconciled_without_sending_it_again(
    managed: tuple[FakeIssue, Path],
) -> None:
    """Jira took the body and the answer never arrived.

    From the record alone this is indistinguishable from a request that never
    left, which is the reason nothing is concluded from the record. The remote
    holds the candidate, so the write happened, and the work left is to bind the
    file to it.
    """

    issue, path = managed
    path.write_bytes(EDITED)
    issue.lose_response = True

    with pytest.raises(Interrupted):
        _push(issue, path)

    assert issue.puts == 1
    pending = read_journal(path)
    assert pending is not None
    # Still `planned`: the acknowledgement is recorded after the call returns,
    # and it never returned.
    assert pending.stage == description_journal.PLANNED

    issue.lose_response = False
    result = _push(issue, path)

    assert result["status"] == "reconciled"
    assert result["local_edit_pending"] is False
    assert issue.puts == 1
    assert not journal_path(path).exists()

    binding = read_binding(path)
    assert binding is not None
    assert binding.source_sha256 == source_sha256(issue.description)


def test_a_failed_readback_is_reconciled_without_sending_it_again(
    managed: tuple[FakeIssue, Path],
) -> None:
    """The write was acknowledged and the confirming read did not come back.

    Recorded as acknowledged, because that is the one thing the next run cannot
    learn from the remote when the body is missing -- and it changes the answer.
    """

    issue, path = managed
    path.write_bytes(EDITED)
    # Reads in order: the pull's, this run's fresh read, the re-read immediately before the write,
    # then the readback. Injecting failure by ordinal is fragile in exactly this way — R3-2's fix
    # added the third read, and a test that counts reads positionally silently moved to a different
    # subject. It is 4 because the readback is the one this test is about.
    issue.fail_read_number = 4

    with pytest.raises(Interrupted):
        _push(issue, path)

    assert issue.puts == 1
    pending = read_journal(path)
    assert pending is not None
    assert pending.stage == description_journal.BODY_APPLIED_READBACK_PENDING

    issue.fail_read_number = None
    result = _push(issue, path)

    assert result["status"] == "reconciled"
    assert issue.puts == 1
    assert not journal_path(path).exists()


def test_a_record_left_behind_by_a_failed_cleanup_reconciles_itself(
    managed: tuple[FakeIssue, Path],
) -> None:
    """The publish succeeded and the record was not removed.

    Removing it is a tidy-up, so it must not be able to fail a publish. What
    makes that safe is this: the next run finds the remote already holding the
    candidate and takes the same path a lost response takes.
    """

    issue, path = managed
    path.write_bytes(EDITED)
    result = _push(issue, path)
    assert result["status"] == "updated"

    # Exactly what a cleanup that did not happen leaves behind.
    description_journal.write_journal(
        path,
        description_journal.new_operation(
            issue_id=issue.issue_id,
            issue_key="DEMO600-1",
            authority="md",
            base_sha256=source_sha256(PLAIN),
            candidate_sha256=source_sha256(issue.description),
            attachments=(),
        ).applied(),
    )

    again = _push(issue, path)

    assert again["status"] == "reconciled"
    assert issue.puts == 1
    assert not journal_path(path).exists()


# --------------------------------------------------------------------------
# 6-8. The world moved while the write was pending
# --------------------------------------------------------------------------


def test_a_remote_that_matches_neither_side_is_a_conflict(managed: tuple[FakeIssue, Path]) -> None:
    """The issue holds neither what the write started from nor what it would
    have sent, so nobody can say whether the write landed and was edited or
    never landed at all. Both readings lose somebody's text if guessed at."""

    issue, path = managed
    path.write_bytes(EDITED)
    issue.refuse_write = True
    with pytest.raises(Interrupted):
        _push(issue, path)

    issue.refuse_write = False
    issue.description = "h2. Title\n\nsomebody else was here\n"

    with pytest.raises(ConflictError) as caught:
        _push(issue, path)

    assert caught.value.context["reason"] == "description_operation_remote_diverged"
    assert issue.puts == 0


def test_a_body_jira_accepted_and_no_longer_holds_is_not_sent_again(
    managed: tuple[FakeIssue, Path],
) -> None:
    """The stage is the only thing that separates this from a write that never
    left, and the two want opposite actions.

    Jira acknowledged the write and the issue is back to the body it started
    from, so something undid it. Sending it again would be arguing with whoever
    did, silently, on a schedule set by how often somebody runs push.
    """

    issue, path = managed
    path.write_bytes(EDITED)
    # 4, not 3: the pre-write re-read joined the sequence. See the note above.
    issue.fail_read_number = 4
    with pytest.raises(Interrupted):
        _push(issue, path)
    assert read_journal(path).stage == description_journal.BODY_APPLIED_READBACK_PENDING  # type: ignore[union-attr]

    issue.fail_read_number = None
    issue.description = PLAIN
    # The interrupted run did send one. What must not happen is a second.
    already_sent = issue.puts

    with pytest.raises(ConflictError) as caught:
        _push(issue, path)

    assert caught.value.context["reason"] == "description_operation_applied_but_absent"
    assert issue.puts == already_sent


def test_attachments_that_changed_under_a_pending_write_are_a_conflict(
    managed: tuple[FakeIssue, Path],
) -> None:
    """The pending write was proven against a set of attachments that is no
    longer the set the issue holds, so the proof is about a state that has
    stopped existing."""

    issue, path = managed
    path.write_bytes(EDITED)
    issue.refuse_write = True
    with pytest.raises(Interrupted):
        _push(issue, path)

    issue.refuse_write = False
    issue.attachments = [{"id": "42", "filename": "new.png", "created": "", "size": "10"}]

    with pytest.raises(ConflictError) as caught:
        _push(issue, path)

    assert caught.value.context["reason"] == "description_operation_attachments_changed"
    assert issue.puts == 0


def test_a_local_edit_over_a_write_that_never_landed_starts_a_new_operation(
    managed: tuple[FakeIssue, Path],
) -> None:
    """Nothing landed and the file has moved on, so the recorded write will
    never be completed by anyone.

    Concluding it from the remote is not the same as ignoring it: the remote
    still holds the base, which is the proof that it did not happen. Refusing
    instead would leave a file nobody can publish without deleting state by
    hand, and hand-editing recovery state is how recoveries go wrong.
    """

    issue, path = managed
    path.write_bytes(EDITED)
    issue.refuse_write = True
    with pytest.raises(Interrupted):
        _push(issue, path)
    stale = read_journal(path)
    assert stale is not None

    issue.refuse_write = False
    path.write_bytes(b"## Title\n\na different rewrite\n")
    result = _push(issue, path)

    assert result["status"] == "updated"
    assert result["operation_id"] != stale.operation_id
    assert issue.puts == 1
    assert "a different rewrite" in issue.description


def test_a_local_edit_over_a_write_that_landed_settles_before_publishing(
    managed: tuple[FakeIssue, Path],
) -> None:
    """Two things are true and only one may be acted on.

    The interrupted write landed, and the file has since changed. Publishing
    both in one step would send an edit whose baseline the caller has never
    seen. So the landed write is settled, the file is rebound to it, and the
    local edit is left for a second run -- which is a rerun of the same command,
    not a repair.
    """

    issue, path = managed
    path.write_bytes(EDITED)
    issue.lose_response = True
    with pytest.raises(Interrupted):
        _push(issue, path)

    issue.lose_response = False
    path.write_bytes(b"## Title\n\nand then some more\n")
    settled = _push(issue, path)

    assert settled["status"] == "reconciled"
    assert settled["local_edit_pending"] is True
    assert issue.puts == 1
    action = settled["next_actions"][0]
    assert action["argv"][:5] == ["jira", "issue", "description", "md", "push"]
    assert "--md-file" in action["argv"]
    assert not any("<" in part for part in action["argv"])

    published = _push(issue, path)

    assert published["status"] == "updated"
    assert issue.puts == 2
    assert "and then some more" in issue.description


# --------------------------------------------------------------------------
# What is proven is what is sent is what came back
# --------------------------------------------------------------------------


def test_the_candidate_proven_is_sent_and_is_what_the_readback_holds(
    managed: tuple[FakeIssue, Path],
) -> None:
    """One document across all three, checked as one statement rather than as
    three separate assertions in three separate tests."""

    issue, path = managed
    path.write_bytes(EDITED)
    candidate = description_push.build_candidate(EDITED.decode("utf-8"))

    result = _push(issue, path)

    assert issue.sent == [candidate]
    assert issue.description == candidate
    assert result["description_matches_sent"] is True
    binding = read_binding(path)
    assert binding is not None
    assert binding.base_wiki == candidate


# --------------------------------------------------------------------------
# What the record is allowed to contain
# --------------------------------------------------------------------------


def test_the_record_holds_no_part_of_anybody_s_description(tmp_path: Path) -> None:
    """Recovery state outlives the process, gets copied into bug reports, and is
    read by people who were never shown the issue. So it carries what is needed
    to decide and nothing that would be a leak if it travelled.

    Asserted on a body with distinctive words in it: a check that only looks for
    the absence of a `description` key would pass while the text sat in some
    other field.
    """

    secret = "PATIENT SURNAME Kowalczyk INTERNAL-ONLY"
    issue = FakeIssue(f"h2. Title\n\n{secret}\n")
    path = tmp_path / "description.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    path.write_bytes(f"## Title\n\n{secret} and more\n".encode())
    issue.refuse_write = True
    with pytest.raises(Interrupted):
        _push(issue, path)

    raw = journal_path(path).read_text(encoding="utf-8")

    for word in secret.split():
        assert word not in raw
    assert "jira.example.com" not in raw
    assert set(json.loads(raw)) == {
        "schema",
        "operation_id",
        "issue_id",
        "issue_key",
        "authority",
        "base_sha256",
        "candidate_sha256",
        "attachments_sha256",
        "stage",
        "proof_bundle",
    }


def test_the_record_does_not_grow_with_the_description(tmp_path: Path) -> None:
    """Every field is a fixed-width hash, a short enum or an id, so a description
    of any size produces a record of the same size.

    Measured as a difference rather than against a fixed budget: a budget large
    enough to be safe would also be large enough to hide a body being copied in.
    """

    # No trailing space on either paragraph. Not incidental: a body whose line
    # ends in a space is admitted by the grade, whose round trip ignores
    # trailing whitespace, and then refused by the publish, whose comparison is
    # exact -- so a fixture with one would fail here for a reason that has
    # nothing to do with how big a journal gets.
    small = FakeIssue("h2. T\n\nx\n")
    big = FakeIssue("h2. T\n\n" + " ".join(["prose"] * 4000) + "\n")
    sizes = []
    for index, issue in enumerate((small, big)):
        path = tmp_path / f"description{index}.md"
        description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
        path.write_bytes(path.read_bytes() + b"\nedited\n")
        issue.refuse_write = True
        with pytest.raises(Interrupted):
            _push(issue, path)
        sizes.append(journal_path(path).stat().st_size)

    assert sizes[0] == sizes[1]
    assert sizes[0] < 1024


def test_a_hand_changed_record_is_refused_rather_than_trusted(
    managed: tuple[FakeIssue, Path],
) -> None:
    """A journal is a file somebody can edit, and the field worth editing is the
    one that turns "the remote diverged" into "nothing landed, go ahead".

    Without the bundle, rewriting `base_sha256` to match a remote somebody else
    wrote would make this resume on top of their work.
    """

    issue, path = managed
    path.write_bytes(EDITED)
    issue.refuse_write = True
    with pytest.raises(Interrupted):
        _push(issue, path)

    payload = json.loads(journal_path(path).read_text(encoding="utf-8"))
    payload["base_sha256"] = source_sha256("something else entirely")
    journal_path(path).write_text(json.dumps(payload), encoding="utf-8")

    issue.refuse_write = False
    with pytest.raises(ValidationError) as caught:
        _push(issue, path)

    assert caught.value.context["reason"] == "description_operation_unreadable"
    assert issue.puts == 0


def test_an_unreadable_record_stops_the_publish(managed: tuple[FakeIssue, Path]) -> None:
    """A record present and not understood is the one thing this must not shrug
    at: carrying on would publish without knowing whether an earlier attempt is
    already out there."""

    issue, path = managed
    path.write_bytes(EDITED)
    journal_path(path).write_text("{ not json", encoding="utf-8")

    with pytest.raises(ValidationError) as caught:
        _push(issue, path)

    assert caught.value.context["reason"] == "description_operation_unreadable"
    assert issue.puts == 0


def test_a_record_belonging_to_another_issue_is_refused(managed: tuple[FakeIssue, Path]) -> None:
    issue, path = managed
    path.write_bytes(EDITED)
    issue.refuse_write = True
    with pytest.raises(Interrupted):
        _push(issue, path)

    issue.refuse_write = False
    issue.issue_id = "99999"

    with pytest.raises(ConflictError) as caught:
        _push(issue, path)

    assert caught.value.context["reason"] == "description_operation_issue_mismatch"
    assert issue.puts == 0
