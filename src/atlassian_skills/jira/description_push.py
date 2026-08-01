"""Publish a managed Markdown description, or refuse with the reason.

The write half. Still hidden, and still not reachable from the CLI.

The order below is the design, not an implementation detail:

    1  build the candidate      Markdown back to wiki, once, and keep it
    2  re-read the issue        fresh, immediately before deciding
    3  finish any pending write from a run that did not get to say how it went
    4  re-check the binding     did the description move while we were thinking?
    5  prove the candidate      against that fresh read, not against the binding
    6  journal the intent       before the request, or a crash leaves no record
    7  PUT
    8  read back                what did the server actually keep?
    9  drop the journal         the operation has an answer

Steps 4 and 5 are separate questions and both are needed. Step 4 asks whether
the body this file was derived from is still what the issue holds; step 5 asks
whether the candidate is safe against it. A publish that only asks the first
sends an unproven document; one that only asks the second overwrites a
concurrent edit with something perfectly well-formed.

The proof runs AFTER the fresh read on purpose. Attachment identity is part of
what it checks, and an attachment that moved since the pull is only visible in
a current read -- proving against the binding alone would certify a state that
is no longer there.

Step 3 runs BEFORE step 4 because a write that landed leaves the remote holding
the candidate, which is not what the binding says -- so the stale check would
fire first and report a concurrent editor where there was only our own
interrupted run.

The candidate built in step 1 is the candidate published in step 7. Rebuilding
it after the checks would mean proving one document and publishing another --
which is the exact shape of defect this project spent a corpus finding.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import cfxmark

from atlassian_skills.core.errors import ConflictError, NotFoundError, ValidationError
from atlassian_skills.jira.description_binding import (
    DescriptionBinding,
    read_binding,
    source_sha256,
    write_binding,
)
from atlassian_skills.jira.description_grade import attachment_filenames, identity_values
from atlassian_skills.jira.description_io import (
    ambiguous_attachment_references,
    attachment_identity,
    baseline_markdown,
    read_exact,
    read_issue,
)
from atlassian_skills.jira.description_journal import (
    BODY_APPLIED_READBACK_PENDING,
    DescriptionOperation,
    attachment_plan_sha256,
    clear_journal,
    journal_path,
    new_operation,
    read_journal,
    write_journal,
)
from atlassian_skills.jira.read_projection import comparable

#: What this workflow can and cannot promise about a concurrent edit, in the payload
#: rather than only in a design document.
#:
#: `DECISIONS.md` D-4 accepts the residual window: Jira Server/DC's issue update endpoint
#: has no precondition -- no `If-Match`, no version field -- so between the fresh read and
#: the server applying the PUT there is an interval no client-side check can close. A save
#: landing there is overwritten and the write still reports success. Found as R3-2.
#:
#: Accepting it is not the same as hiding it. A caller deciding whether to publish is
#: entitled to know the guarantee is best-effort rather than conditional, and a receipt
#: saying `updated` with nothing beside it reads as a conditional write that succeeded.
#: Six months later the receipt is all anyone has.
CONCURRENCY_DISCLOSURE: dict[str, object] = {
    "guarantee": "best_effort",
    "server_conditional_write": False,
    "detail": (
        "Jira Server/DC's issue update endpoint offers no precondition (no If-Match, no "
        "version field), so a remote save between this workflow's fresh read and the server "
        "applying the write is overwritten and cannot be detected. The description is re-read "
        "immediately before the write, which narrows the window to one round trip; it does not "
        "close it."
    ),
    "mitigation": "re-read immediately before the write",
}


def build_candidate(markdown: str) -> str:
    """The wiki this Markdown would become. One conversion, reused throughout."""

    try:
        return cfxmark.to_jira_wiki(markdown, input_format="markdown").jira_wiki or ""
    except Exception as error:  # noqa: BLE001 - a body we cannot render is one we cannot publish
        raise ValidationError(
            "This Markdown cannot be rendered as Jira wiki, so there is nothing safe to publish.",
            hint="Edit the description as exact wiki instead, which needs no conversion.",
            context={"reason": "candidate_conversion_failed", "detail": type(error).__name__},
        ) from error


def assess_candidate(
    base_wiki: str,
    base_markdown: str,
    candidate: str,
    *,
    remote_attachments: tuple[dict[str, str], ...] = (),
    bound_attachments: tuple[dict[str, str], ...] = (),
) -> dict[str, Any]:
    """Prove this candidate against everything that could make it wrong.

    An earlier version compared the SET OF KINDS present before and after, which
    is close to no check at all. It passed a candidate that kept one of two
    mentions, one that swapped `[~alice]` for `[~bob]`, and one that changed an
    attachment's filename -- all three detach something from the issue, and all
    three "still have mentions" or "still have an attachment reference".

    Five independent questions, each fail-closed:

      identity      the same VALUES, the same number of times
      round trip    does the base Markdown still reproduce the base wiki?
      drift         if not, the converter changed under this file, and the
                    candidate is a mix of two converters' output
      attachments   is the identity set the file was bound to still the one the
                    issue holds?
      ambiguity     does every attachment reference resolve to one attachment?
      renderable    did the candidate come out of a conversion at all?

    The round-trip question is the one that is easy to leave out and hardest to
    recover from. The file on disk is Markdown; what publishes is that Markdown
    converted. If converting the file's OWN base no longer reproduces the wiki
    it was pulled from, then every difference this proof measures is being
    measured against the wrong baseline -- and a clean result means nothing.
    """

    findings: list[str] = []

    before = identity_values(base_wiki)
    after = identity_values(candidate)
    lost: dict[str, list[str]] = {}
    for kind, values in before.items():
        kept = Counter(after.get(kind, ()))
        missing = sorted((Counter(values) - kept).elements())
        if missing:
            lost[kind] = missing
    if lost:
        findings.append("identity_values_lost")

    # The converter that produced `base_markdown` against the one running now.
    # Compared on the wiki side because that is what publishes.
    whitespace_only_base = False
    try:
        reproduced = cfxmark.to_jira_wiki(base_markdown, input_format="markdown").jira_wiki or ""
    except Exception:  # noqa: BLE001 - a base we cannot re-render is a base we cannot trust
        reproduced = ""
        findings.append("base_round_trip_failed")
    else:
        if base_markdown and reproduced != base_wiki:
            # Byte-different and word-identical is not drift. A body whose line
            # ends in a space is admitted by the grade, which normalises exactly
            # this much, and an exact comparison here called that a converter
            # defect -- so an ordinary description became unpublishable and the
            # reason given named a bug that does not exist.
            if comparable(reproduced) == comparable(base_wiki):
                whitespace_only_base = True
            else:
                findings.append("converter_drift")

    # Only the files this description points at. Comparing the WHOLE attachment
    # set answered a question nobody asked: someone drops a log on the issue
    # while a description is being edited, and the push is refused with a
    # finding about attachments on a body that references none. What this check
    # is for is whether the references in this body still mean what they meant,
    # and an attachment nothing references cannot change that answer.
    #
    # Both sides of the edit, because a reference the candidate ADDS has to be
    # resolved against the same reading.
    referenced = set(attachment_filenames(base_wiki)) | set(attachment_filenames(candidate))
    bound = {
        (item.get("id", ""), item.get("filename", ""))
        for item in bound_attachments
        if item.get("filename") in referenced
    }
    live = {
        (item.get("id", ""), item.get("filename", ""))
        for item in remote_attachments
        if item.get("filename") in referenced
    }
    attachments_changed = bound != live
    if attachments_changed:
        findings.append("attachment_identity_changed")

    # Checked against the fresh read, and so it can start being true between a
    # pull and a push: someone uploads a second `diagram.png` and the reference
    # that resolved to one attachment now resolves to neither in particular.
    ambiguous = ambiguous_attachment_references(candidate, remote_attachments)
    if ambiguous:
        findings.append("attachment_filename_ambiguous")

    # Identity compares `Counter(before) - Counter(after)`, which is empty
    # whenever the candidate keeps everything the base had -- including when it
    # keeps all of it and adds one more. So an ADDED reference passed every
    # check above, while `SKILL.md` and the CHANGELOG both said adding one is
    # refused. It publishes as a broken image and the caller is told `updated`.
    live_filenames = {item.get("filename", "") for item in remote_attachments}
    unresolved = sorted({name for name in attachment_filenames(candidate) if name not in live_filenames})
    if unresolved:
        findings.append("attachment_reference_unresolved")

    return {
        "identity_before": {kind: list(values) for kind, values in before.items()},
        "identity_after": {kind: list(values) for kind, values in after.items()},
        "identity_lost": {kind: list(values) for kind, values in lost.items()},
        "base_round_trip_reproduces_source": not base_markdown or reproduced == base_wiki,
        # Reported rather than swallowed. The base does not round trip byte for
        # byte, and the difference is whitespace -- allowed, and said out loud,
        # because "the bytes we hold are not the bytes we would send" is a fact a
        # reader is entitled to whether or not it blocks anything.
        "base_round_trip_whitespace_only": whitespace_only_base,
        "attachment_identity_changed": attachments_changed,
        "ambiguous_attachment_references": list(ambiguous),
        "unresolved_attachment_references": unresolved,
        "findings": findings,
        "safe": not findings,
    }


def classify_change(remote: str, candidate: str) -> str:
    """What publishing this candidate would do to the stored body.

    Three answers, and the middle one is the reason this function exists:

        no_change                the stored bytes already are the candidate
        whitespace_only_change   the words are identical, the bytes are not
        content_change           something a reader would notice

    A first push of a `markdown_ready` body can land in the middle. The grade is
    decided on a normalised round trip, so a trailing space or a blank line can
    differ between what Jira holds and what the Markdown converts to. Publishing
    that is allowed -- refusing would make most bodies unmanageable over
    whitespace -- but it is never allowed to be silent, because a caller told
    only "updated" has no way to know the stored bytes moved.
    """

    if candidate == remote:
        return "no_change"
    if comparable(candidate) == comparable(remote):
        return "whitespace_only_change"
    return "content_change"


#: One sentence per finding, so a refusal explains the finding it actually made.
#: A single hint covering six of them described identity to somebody whose only
#: problem was a filename, and that reader has no way to tell which half applies.
_HINTS = {
    "identity_values_lost": (
        "A mention, link or attachment reference this description had is missing from the candidate."
    ),
    "base_round_trip_failed": (
        "The Markdown this file was pulled as no longer converts, so there is no baseline to prove against."
    ),
    "converter_drift": (
        "The converter no longer reproduces the wiki this file was pulled from, "
        "so every difference measured here is measured against the wrong baseline."
    ),
    "attachment_identity_changed": (
        "An attachment this description references was replaced on the issue after the pull."
    ),
    "attachment_filename_ambiguous": (
        "Two attachments share a filename this description references, so the reference names neither in particular."
    ),
    "attachment_reference_unresolved": (
        "This candidate references a file the issue does not have. Descriptions carry references; they never upload."
    ),
}

#: Re-pulling is the answer to a stale baseline and it overwrites the file, so
#: every finding it answers asks first.
_STALE_BASELINE = frozenset({"base_round_trip_failed", "converter_drift", "attachment_identity_changed"})


def _next_actions(proof: dict[str, Any], key: str, path: Path) -> list[dict[str, Any]]:
    """What to run next, as argv rather than as a description of argv."""

    findings = set(proof["findings"])
    actions: list[dict[str, Any]] = []
    for name in proof["unresolved_attachment_references"]:
        actions.append(
            {
                "label": f"attach {name} to the issue, then push again",
                "argv": ["jira", "attachment", "upload", key, name],
                "requires_user_approval": True,
            }
        )
    if findings & _STALE_BASELINE:
        actions.append(
            {
                "label": "re-pull so the baseline is what the issue holds now — THIS DISCARDS THE LOCAL EDIT",
                "argv": ["jira", "issue", "description", "md", "pull", key, "--output", str(path)],
                "requires_user_approval": True,
            }
        )
    return actions


def push_md(
    client: Any,
    key: str,
    path: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Publish the managed Markdown file as the issue's description."""

    if not path.exists():
        raise NotFoundError(f"No such file: {path}")
    markdown = read_exact(path)
    binding = read_binding(path)

    if binding is None:
        raise ValidationError(
            "This Markdown file has no binding, so there is no wiki baseline to publish against.",
            hint="Pull it with 'atls jira issue description md pull'.",
            context={"reason": "description_binding_missing", "path": str(path), "key": key},
        )
    if binding.authority != "md":
        # The wiki side owns this directory. Publishing Markdown here would race
        # a workflow that believes it holds the authoritative copy.
        raise ValidationError(
            "The exact wiki representation is authoritative in this directory, so Markdown cannot publish from it.",
            hint="Publish with the wiki workflow, or move authority deliberately.",
            context={
                "reason": "wiki_is_authoritative",
                "path": str(path),
                "authority": binding.authority,
            },
        )

    candidate = build_candidate(markdown)
    pending = _read_journal(path)

    # Fresh, and deliberately AFTER the candidate is built: the window between
    # deciding and writing is what a stale check is trying to make small. The
    # proof runs against THIS read, not against the binding alone, so an
    # attachment that moved since the pull is part of what is being proven.
    fields, remote = read_issue(client, key)

    if pending is not None:
        settled = _settle_pending(pending, path, binding, markdown, candidate, fields, remote, key=key, dry_run=dry_run)
        if settled is not None:
            return settled
        # The pending write provably never landed and describes something this
        # run is not doing. It has been concluded rather than ignored, and what
        # follows is an ordinary publish.
        binding = read_binding(path) or binding

    _assert_still_bound(binding, fields, remote, path=path, key=key)

    proof = assess_candidate(
        binding.base_wiki,
        binding.base_markdown,
        candidate,
        remote_attachments=attachment_identity(fields),
        bound_attachments=binding.attachments,
    )
    if not proof["safe"]:
        raise ValidationError(
            "This candidate cannot be proven safe to publish: " + ", ".join(proof["findings"]) + ".",
            hint=" ".join(_HINTS[finding] for finding in proof["findings"] if finding in _HINTS)
            or "Edit the description as exact wiki instead, which needs no conversion.",
            context={
                "reason": "candidate_proof_failed",
                "path": str(path),
                "key": key,
                "next_actions": _next_actions(proof, fields["key"], path),
                **proof,
            },
        )

    change = classify_change(remote, candidate)
    if change == "no_change":
        return {
            "status": "no_change",
            "key": fields["key"],
            "updated": str(fields.get("updated") or ""),
            "change_class": change,
        }

    if dry_run:
        return {
            "status": "dry_run",
            "concurrency": dict(CONCURRENCY_DISCLOSURE),
            "method": "PUT",
            "key": fields["key"],
            # Said before the write, not only after it. A caller running a dry
            # run to decide whether to publish is exactly the caller who needs
            # to know the only difference is whitespace.
            "change_class": change,
            "would_write_sha256": source_sha256(candidate),
            "remote_sha256": source_sha256(remote),
            "proof": proof,
        }

    # Before the request. A crash between here and the PUT leaves a record
    # saying what was going to be sent, which is what makes the next run able to
    # tell a lost response from a request that never left.
    operation = new_operation(
        issue_id=fields["id"],
        issue_key=fields["key"],
        authority="md",
        base_sha256=source_sha256(remote),
        candidate_sha256=source_sha256(candidate),
        attachments=attachment_identity(fields),
    )
    write_journal(path, operation)

    # One more read, immediately before the write, and nothing between them.
    #
    # Review R3 (R3-2) found that the whole prove step sat between the fresh read and an
    # unconditional PUT, so a save landing anywhere in that span was overwritten and reported as
    # `updated`. Jira Server/DC has no precondition on issue update -- no `If-Match`, no version
    # field -- so the window cannot be closed by asking the server to refuse.
    #
    # What it can be is *emptied of everything a client can observe*. Re-reading here means every
    # concurrent save that has reached the server by this moment is caught, refused with the same
    # named reason, and costs PUT 0. What remains is the interval between this response and the
    # server applying the write, which no client on an API without preconditions can see -- a
    # property of the endpoint rather than a choice made here, and named in P7's evidence rather
    # than left for somebody to discover.
    _immediate_fields, immediate = read_issue(client, fields["key"])
    if source_sha256(immediate) != source_sha256(remote):
        raise ConflictError(
            "The description changed on the server while this push was being prepared.",
            hint=(
                "Read the current description, merge the two by meaning, then push. "
                "Re-pulling would discard the local edit."
            ),
            context={
                "reason": "description_remote_changed",
                "path": str(path),
                "key": fields["key"],
                "pulled_sha256": source_sha256(remote),
                "remote_sha256": source_sha256(immediate),
                # Distinguishes this from the earlier check, which compares against the binding.
                # Same remedy, different moment, and a caller counting refusals wants to know
                # that the edit arrived mid-push rather than before it started.
                "detected": "immediately_before_write",
            },
        )

    client.update_issue(fields["key"], fields={"description": candidate})
    # The server acknowledged. Recorded because the next run cannot otherwise
    # tell "the write never landed" from "the write landed and was undone", and
    # those want opposite actions.
    write_journal(path, operation.applied())

    after_fields, after = read_issue(client, fields["key"])
    # The candidate that was PROVEN is the candidate that was SENT. This asks
    # the remaining question: is it also the one the server kept?
    readback = classify_change(after, candidate)
    if readback == "content_change":
        # Reported as a success before, with the answer carried in a field
        # nothing had to read. A caller saw `status: updated` and exit 0 while
        # the issue held another person's description -- the shape of every
        # concurrent-editor collision, arriving as good news.
        #
        # The journal stays. It is the only record that this run's candidate was
        # sent, and the next run needs it to tell a collision from a fresh edit.
        raise ConflictError(
            "Jira is not holding the description this push sent.",
            hint=(
                "Something wrote the description between this write and the read after it. "
                "Read the issue and decide what should survive; the pending operation beside "
                "this file records what was sent."
            ),
            context={
                "reason": "description_readback_mismatch",
                "path": str(path),
                "key": after_fields["key"],
                "operation_id": operation.operation_id,
                "sent_sha256": source_sha256(candidate),
                "readback_sha256": source_sha256(after),
            },
        )
    _rebind(path, binding, after_fields, after, markdown)
    clear_journal(path)
    return {
        "status": "updated",
        # `updated` alone reads as a conditional write that succeeded. It was not
        # conditional; see `CONCURRENCY_DISCLOSURE`.
        "concurrency": dict(CONCURRENCY_DISCLOSURE),
        "key": after_fields["key"],
        "updated": str(after_fields.get("updated") or ""),
        "operation_id": operation.operation_id,
        "change_class": change,
        # False when the server normalised whitespace on the way in. Said out
        # loud rather than smoothed over: the bytes on the issue are not the
        # bytes that were sent, and a later byte comparison will find that.
        "description_matches_sent": readback == "no_change",
        "readback_class": readback,
        "proof": proof,
    }


def _rebind(
    path: Path,
    binding: DescriptionBinding,
    fields: dict[str, Any],
    stored: str,
    markdown: str,
) -> None:
    """Bind the file to what the server KEPT, not to what was sent."""

    write_binding(
        path,
        DescriptionBinding(
            issue_id=fields["id"],
            issue_key=fields["key"],
            site=binding.site,
            remote_updated=str(fields.get("updated") or ""),
            source_sha256=source_sha256(stored),
            authority="md",
            base_wiki=stored,
            base_markdown=baseline_markdown(stored, markdown),
            grade=binding.grade,
            attachments=attachment_identity(fields),
        ),
    )


def _read_journal(path: Path) -> DescriptionOperation | None:
    """The pending operation, or a refusal naming what is unreadable.

    A journal present and not understood is the one thing this must not shrug
    at: continuing would publish without knowing whether an earlier attempt is
    already out there.
    """

    try:
        return read_journal(path)
    except (OSError, TypeError, ValueError) as error:
        # `TypeError` is the one a newer atls produces: `DescriptionOperation(**known)`
        # rejects a field this version has never heard of. Left uncaught it went
        # out as a traceback on stderr, and a `--format=json` consumer got an
        # empty stdout with no envelope to read. `read_binding` already treats
        # the same situation as something to decide about rather than crash on.
        raise ValidationError(
            "There is a pending description operation beside this file that cannot be read.",
            hint=(
                "It records a publish that may or may not have reached Jira. "
                "Read the issue's description and compare it with this file before publishing again."
            ),
            context={
                "reason": "description_operation_unreadable",
                "path": str(journal_path(path)),
                "detail": type(error).__name__,
            },
        ) from error


def _settle_pending(
    operation: DescriptionOperation,
    path: Path,
    binding: DescriptionBinding,
    markdown: str,
    candidate: str,
    fields: dict[str, Any],
    remote: str,
    *,
    key: str,
    dry_run: bool,
) -> dict[str, Any] | None:
    """Decide a recorded write from the remote, and return None to carry on.

    Everything here is decided by comparing the fresh read with the two hashes
    the journal carries. The journal never says what happened -- it says what
    was going to happen, and which of the remote's possible answers that makes
    recoverable.
    """

    if operation.issue_id and fields["id"] and operation.issue_id != fields["id"]:
        raise ConflictError(
            "The pending operation beside this file belongs to a different issue.",
            hint="Check the key. Publishing now could finish somebody else's write against this issue.",
            context={
                "reason": "description_operation_issue_mismatch",
                "path": str(journal_path(path)),
                "operation_issue_id": operation.issue_id,
                "target_issue_id": fields["id"],
                "requested_key": key,
            },
        )

    remote_sha = source_sha256(remote)
    candidate_sha = source_sha256(candidate)

    if remote_sha == operation.candidate_sha256:
        # The write landed. Whether its response arrived is not something the
        # remote can be asked, and not something that changes what to do.
        local_matches = candidate_sha == remote_sha
        if dry_run:
            return {
                "status": "reconciled",
                "key": fields["key"],
                "operation_id": operation.operation_id,
                "would_mutate": True,
                "local_edit_pending": not local_matches,
                "puts": 0,
            }
        _rebind(path, binding, fields, remote, markdown)
        clear_journal(path)
        result: dict[str, Any] = {
            "status": "reconciled",
            "key": fields["key"],
            "updated": str(fields.get("updated") or ""),
            "operation_id": operation.operation_id,
            "description_matches_sent": True,
            # True when the file moved on after the write landed. Reported and
            # stopped here rather than published in the same breath: recovering
            # and writing are two decisions, and running them together would
            # publish an edit the caller has not seen the baseline for.
            "local_edit_pending": not local_matches,
        }
        if not local_matches:
            result["next_actions"] = [
                {
                    "label": "publish the local edit now that the interrupted write is settled",
                    "argv": ["jira", "issue", "description", "md", "push", fields["key"], "--md-file", str(path)],
                    "requires_user_approval": False,
                }
            ]
        return result

    if remote_sha == operation.base_sha256:
        if operation.stage == BODY_APPLIED_READBACK_PENDING:
            # Jira took the body and the body is not there. Something removed
            # it, and sending it again would be arguing with whoever did.
            raise ConflictError(
                "Jira accepted this description and the issue no longer holds it.",
                hint=(
                    "Something changed the description back after the write. "
                    "Read the issue history to see what, then decide before publishing again."
                ),
                context={
                    "reason": "description_operation_applied_but_absent",
                    "path": str(journal_path(path)),
                    "key": fields["key"],
                    "operation_id": operation.operation_id,
                },
            )
        if candidate_sha != operation.candidate_sha256:
            # Nothing landed, and this run is publishing something else, so the
            # recorded write will never be completed by anyone. Concluding it
            # from the remote is not the same as ignoring it.
            clear_journal(path)
            return None
        if attachment_plan_sha256(attachment_identity(fields)) != operation.attachments_sha256:
            raise ConflictError(
                "The issue's attachments changed while this description write was pending.",
                hint=(
                    "The pending write was proven against a different set of attachments. "
                    "Pull the description again to re-prove it against what the issue holds now."
                ),
                context={
                    "reason": "description_operation_attachments_changed",
                    "path": str(journal_path(path)),
                    "key": fields["key"],
                    "operation_id": operation.operation_id,
                },
            )
        # Same base, same candidate, same attachments: the interrupted write can
        # simply be made again, and the proof below re-establishes it from
        # scratch rather than trusting this record.
        return None

    raise ConflictError(
        "The description changed while a write from this file was pending.",
        hint=(
            "The issue holds neither what the pending write started from nor what it would have sent. "
            "Read the current description and decide what should survive before publishing."
        ),
        context={
            "reason": "description_operation_remote_diverged",
            "path": str(journal_path(path)),
            "key": fields["key"],
            "operation_id": operation.operation_id,
            "remote_sha256": remote_sha,
            "operation_base_sha256": operation.base_sha256,
            "operation_candidate_sha256": operation.candidate_sha256,
        },
    )


def _assert_still_bound(
    binding: DescriptionBinding,
    fields: dict[str, Any],
    remote: str,
    *,
    path: Path,
    key: str,
) -> None:
    if binding.issue_id and fields["id"] and binding.issue_id != fields["id"]:
        raise ValidationError(
            "This file was pulled from a different issue than the one being pushed to.",
            hint="Check the key, or pull the intended issue again.",
            context={
                "reason": "description_binding_issue_mismatch",
                "path": str(path),
                "bound_issue_id": binding.issue_id,
                "target_issue_id": fields["id"],
                "requested_key": key,
            },
        )
    if source_sha256(remote) != binding.source_sha256:
        raise ConflictError(
            "The description changed on the server after this file was pulled.",
            hint=(
                "Read the current description, merge the two by meaning, then push. "
                "Re-pulling would discard the local edit."
            ),
            context={
                "reason": "description_remote_changed",
                "path": str(path),
                "key": fields["key"],
                "pulled_updated": binding.remote_updated,
                "remote_updated": str(fields.get("updated") or ""),
                "pulled_sha256": binding.source_sha256,
                "remote_sha256": source_sha256(remote),
            },
        )


__all__ = ["assess_candidate", "build_candidate", "classify_change", "push_md"]
