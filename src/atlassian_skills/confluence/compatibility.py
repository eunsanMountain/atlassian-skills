"""Turn "what would Markdown drop?" into something an agent can act on.

cfxmark answers the measurement question -- regenerate the page from its own
Markdown, report what did not come back. This module answers the product one:
given that answer, what is the caller allowed to do next, and what exactly do we
have to tell the author first.

Three things here are deliberate and each of them is a lesson.

**Every status exits 0.** These are classifications, not failures. A page that
cannot be published from Markdown is a fact about the page, and an agent that
sees a non-zero exit treats it as a broken command and retries or gives up.
Genuine failures -- auth, network, a stale version -- keep their existing codes.

**`converter_fix_required` is not a consent prompt.** When our converter drops
something Markdown could have carried, the author loses nothing once we fix it,
so asking them to approve the loss would be asking them to consent to our defect.
It is separated from `migration_required` for that reason alone.

**Every next step is an argv, never a sentence.** "Use the XHTML workflow" leaves
the reader to invent a command, which is exactly the dead end this whole design
exists to remove. If we cannot name the command, we have not finished the
thought.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import cfxmark
from cfxmark.compatibility import (
    CONVERTER_GAP_BLOCKED,
    MARKDOWN_IDENTITY_BOUND,
    MARKDOWN_LOSSLESS,
    NAMED_LOSS_ATTRIBUTES,
    NAMED_LOSS_CONSENTABLE,
    PLATFORM_EDITOR_CANONICALIZATION,
    UNKNOWN_BLOCKED,
    CompatibilityReport,
    assess_candidate,
    assess_markdown_compatibility,
    canonicalization_sites,
)

from atlassian_skills.confluence import preservation as preservation_module
from atlassian_skills.confluence.diagnostics import canonical_code, severity_for, title_for
from atlassian_skills.confluence.preservation import preservation_for

#: Bumped whenever a consumer would have to change. Pinned in the payload so a
#: caller can refuse a shape it does not understand instead of guessing.
SCHEMA = "atls-compatibility-v1"

#: The profile every atls conversion path uses. Passed explicitly because the
#: answer is only true for the profile that will actually do the publishing --
#: a macro inside a table cell survives the round trip under the default profile
#: and loses its body under this one.
PROFILE: Literal["editable"] = "editable"


@dataclass(frozen=True)
class WorkflowStatus:
    """One row of the mapping from "what did we measure" to "what happens now".

    Written as data rather than a chain of ifs because the same mapping is needed
    by the CLI, the Skill and any downstream workflow adapter, and three
    hand-written copies is
    how they drift apart.
    """

    status: str
    #: Whether the caller has to *choose how to manage this page* -- Markdown or
    #: storage. Not the same question as whether a particular edit's losses need
    #: approving, which is asked at publish time against the actual candidate.
    #: They were both called `requires_user_approval`, in payloads a caller reads
    #: minutes apart, and one name for two decisions is how the wrong one gets
    #: answered.
    workflow_decision_required: bool
    recommended_workflow: str
    summary: str
    #: Whether a pull of a page with this grade may leave a canonical managed
    #: Markdown file on disk (§8.2's second column).
    #:
    #: It lives here, in the mapping every consumer already reads, because the
    #: grade was previously computed correctly and then not acted on: the pull
    #: wrote the file for all five grades. A policy kept anywhere else is a policy
    #: the publisher has to remember to consult.
    #:
    #: The two permitted grades are the ones where the file can be published as it
    #: stands. The three forbidden ones each leave something that looks like a work
    #: product and is not: it cannot be published, and it is what the next person
    #: edits.
    canonical_write_permitted: bool


#: The single mapping. Every consumer reads it; nobody re-derives it.
STATUS_BY_CLASSIFICATION: dict[str, WorkflowStatus] = {
    MARKDOWN_LOSSLESS: WorkflowStatus(
        status="markdown_ready",
        workflow_decision_required=False,
        recommended_workflow="markdown",
        summary="Markdown holds everything on this page.",
        canonical_write_permitted=True,
    ),
    MARKDOWN_IDENTITY_BOUND: WorkflowStatus(
        status="markdown_identity_bound",
        workflow_decision_required=False,
        recommended_workflow="markdown",
        summary=(
            "Markdown holds the content, but this page carries macro or attachment "
            "identity that a regeneration cannot invent. Publishing must go through the "
            "managed path so the identity is carried forward; otherwise every macro's "
            "comments and attachments detach."
        ),
        # **False**, and that is the plan implemented rather than described.
        #
        # §8.2 permits this grade only on a build that has statically registered an
        # identity-preservation capability for the converter/profile and closed it with a
        # contract test on the same public push path. No such registry exists. The row said
        # `True` anyway, with a comment deferring the gate to "P5" -- so 14 of the 55 live
        # pages were written on a runtime judgement the plan says must not be trusted, and
        # the deferral was doing the work the gate should have done.
        #
        # A grade whose precondition is unimplemented writes nothing. When the registry lands
        # this flips back, and what flips it is the registry rather than a comment.
        canonical_write_permitted=False,
    ),
    NAMED_LOSS_CONSENTABLE: WorkflowStatus(
        status="migration_required",
        workflow_decision_required=True,
        recommended_workflow="markdown",
        summary=(
            "Publishing from Markdown would drop specific, named things. They are listed "
            "with counts so the author can decide, once, whether to lose them."
        ),
        # Not until the author has approved the named losses. The approval reuses
        # the migration report fingerprint; §8.2 forbids a plain `--force`.
        canonical_write_permitted=False,
    ),
    CONVERTER_GAP_BLOCKED: WorkflowStatus(
        status="converter_fix_required",
        # Deliberately not an approval: the author loses nothing once this is
        # fixed, so asking them to consent would be asking them to approve our
        # own defect.
        workflow_decision_required=False,
        recommended_workflow="report",
        summary=(
            "Markdown could carry this and our converter does not. Nothing here is the "
            "author's to approve -- it is ours to fix."
        ),
        # No approval route at all. Consent here would be asking the author to
        # approve our defect, so there is nothing to write and nothing to accept.
        canonical_write_permitted=False,
    ),
    UNKNOWN_BLOCKED: WorkflowStatus(
        status="xhtml_required",
        workflow_decision_required=True,
        recommended_workflow="xhtml",
        summary=(
            "This page holds something we cannot classify safely, so Markdown is not a "
            "trustworthy round trip for it. Edit the storage format directly, or accept "
            "that publishing from Markdown may lose what we could not name."
        ),
        # Decided per page by §8.2.1's second axis rather than by this row, which is
        # why the payload overrides it: a page whose unclassifiable structure has a
        # closed preservation capability may be written, and one without may not. The
        # value here is the answer when nothing is proven, which is the safe one.
        canonical_write_permitted=False,
    ),
}


#: §8.2's second column, transcribed from the plan, kept beside the table that
#: implements it so the two can be compared instead of assumed equal.
#:
#: They agree. They did not for one release-evidence cycle: `xhtml_required` said "no
#: canonical write" and this build wrote the file anyway, because 27 of the 55 pages in the
#: live corpus grade that way and the managed publish path demonstrably preserves what the
#: grade could not classify. That was carried as a named divergence until the plan was
#: amended, and §8.2.1 is the amendment -- the grade keeps its meaning and a second axis
#: decides the write, so nothing had to be loosened.
#:
#: For `xhtml_required` this row is the answer when *nothing is proven*, which is what the
#: plan states. `compatibility_payload` overrides it per page from the preservation
#: registry.
PLAN_8_2_CANONICAL_WRITE: dict[str, bool] = {
    "markdown_ready": True,
    # §8.2's row says "예" *conditionally* -- only a build with the registered, contract-tested
    # identity-carry capability may produce this grade at all. With no registry the condition is
    # unmet, so the plan's answer for this build is no write. Transcribed as the conditional
    # resolves today rather than as the row reads in isolation.
    "markdown_identity_bound": False,
    "migration_required": False,
    "converter_fix_required": False,
    "xhtml_required": False,
}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _next_actions(
    page_id: str,
    status: WorkflowStatus,
    report: CompatibilityReport,
    *,
    document_path: str | None,
    storage_path: str | None,
    #: The *computed* permission, not the policy row's. §8.2.1 has two axes and the
    #: row carries only the first: for an `xhtml_required` page the answer depends on
    #: whether a registered preservation capability covers it, which the row cannot
    #: know. Passed in rather than re-derived so the envelope and the actions inside it
    #: cannot disagree -- a caller reading "Markdown may write here" beside a list of
    #: storage commands has to guess which half is true, and the writing half is the
    #: dangerous one to get wrong.
    canonical_write_permitted: bool,
) -> list[dict[str, Any]]:
    """The commands that actually move this page forward, as argv.

    Every path is a real one. An argv carrying `<file>` cannot be run, and the
    skill's central rule is to run what is returned and never invent a command --
    so a placeholder does not merely inconvenience the caller, it puts them in
    the position the rule forbids. When a path is genuinely unknown the action is
    omitted rather than filled in with a guess, and the read-only step below
    keeps the payload from ever being a dead end.
    """

    actions: list[dict[str, Any]] = []
    # This payload describes a page, not a moment: `push --dry-run` asks the same
    # question against a file the caller is holding, and gating the push argv on the
    # grade there would strip the one action a `migration_required` push needs.
    #
    # A pull that wrote nothing is the only caller for whom `document_path` names an
    # absent file, and it is the caller that knows so. It drops those actions itself
    # -- see `_drop_actions_needing_the_document`.
    if (status.recommended_workflow == "markdown" or canonical_write_permitted) and document_path is not None:
        actions.append(
            {
                "label": "see what would change without writing anything",
                "argv": [
                    "confluence",
                    "page",
                    "md",
                    "push",
                    page_id,
                    "--md-file",
                    document_path,
                    "--dry-run",
                    "--format=json",
                ],
                "requires_user_approval": False,
            }
        )
        actions.append(
            {
                "label": "publish the edited Markdown through the managed path",
                "argv": ["confluence", "page", "md", "push", page_id, "--md-file", document_path],
                "requires_user_approval": True,
            }
        )
    # A grade that recommends Markdown and may not write it needs the storage route too, or the
    # caller is told what this page is and given nothing to do about it. That happened the moment
    # `markdown_identity_bound` stopped writing: its only action read the file the pull had not
    # written, the refusal dropped it, and `next_actions` came back empty -- the dead end this
    # payload exists to remove, reached from the other side.
    if not canonical_write_permitted:
        # The managed storage path rather than the raw pair it is built on. Both
        # work; only one records what the document was pulled from, and without
        # that record nothing afterwards can tell an edit from a page that moved.
        if storage_path is not None:
            actions.append(
                {
                    "label": "take this page onto the storage workflow",
                    "argv": ["confluence", "page", "xhtml", "pull", page_id, "--output", storage_path],
                    "requires_user_approval": False,
                }
            )
            actions.append(
                {
                    "label": "check the edited storage document before publishing it",
                    "argv": [
                        "confluence",
                        "page",
                        "xhtml",
                        "push",
                        page_id,
                        "--xhtml-file",
                        storage_path,
                        "--dry-run",
                        "--format=json",
                    ],
                    "requires_user_approval": False,
                }
            )
    if report.classification == CONVERTER_GAP_BLOCKED:
        actions.append(
            {
                "label": "report the converter gap with the findings below",
                "argv": ["confluence", "page", "get", page_id, "--body-repr=storage", "--format=raw"],
                "requires_user_approval": False,
            }
        )
    if not actions:
        # Never a dead end, and never a placeholder. This one needs no path, so
        # it is what remains when the caller gave us nothing to name.
        actions.append(
            {
                "label": "read what this page actually holds",
                "argv": ["confluence", "page", "get", page_id, "--body-repr=storage", "--format=raw"],
                "requires_user_approval": False,
            }
        )
    return actions


#: Flags that make the next token a file the command *reads*. An unwritten path
#: after one of these is a command that fails on the caller's behalf; after
#: `--output` the same path is exactly right, which is why direction is what gets
#: checked rather than the path itself.
CONSUMING_FLAGS = frozenset({"--md-file", "-f", "--xhtml-file", "--body-file", "--file", "--candidate"})


def drop_actions_needing_the_document(actions: list[dict[str, Any]], document_path: str) -> list[dict[str, Any]]:
    """Remove the actions that read a document this caller did not write.

    Called by the pull that graded no-write. Keeping them would hand back
    `md push --md-file <path>` for a path with nothing at it -- worse than a
    placeholder, because a placeholder cannot be run and this one runs and fails
    as though the caller had mistyped something.
    """

    kept = []
    for action in actions:
        argv = action.get("argv", [])
        if any(flag in CONSUMING_FLAGS and value == document_path for flag, value in zip(argv, argv[1:], strict=False)):
            continue
        kept.append(action)
    return kept


def storage_path_for(document_path: str | None) -> str | None:
    """Where a storage copy of this document would go, decided here not by the caller.

    An agent asked to supply this would invent one, and two agents would invent
    two. Beside the Markdown file, same stem: `docs/page.md` -> `docs/page.xhtml`.
    """

    if document_path is None:
        return None
    path = Path(document_path)
    return str(path.with_suffix(".xhtml"))


def _identity_not_proven(status: Any) -> Any:
    """The identity axis's mirror of the preservation rewrite, for when the carry declines.

    `markdown_identity_bound` grants the canonical write only where the registered carry
    covers this page. When it does not, `canonical_write_permitted` flips to `False` --
    and before this, nothing else moved: the payload still said `recommended_workflow:
    markdown` with `workflow_decision_required: False`, so an agent reading the fields it
    is told to read ran the Markdown pull and found no file. The preservation axis got
    this treatment when its capability *applies*; this is the same correction for the
    axis where the capability is *withheld*.
    """

    return replace(
        status,
        workflow_decision_required=True,
        recommended_workflow="xhtml",
        summary=(
            "Markdown holds the content, but this page carries identity the registered carry has not "
            "been proven to keep on a page of this shape, so no canonical Markdown file is written. "
            "Publish through the XHTML workflow, or edit the page so its identity structures are "
            "distinguishable."
        ),
    )


def compatibility_payload(
    page_id: str,
    storage: str,
    *,
    document_path: str | None = None,
    base_artifact: Any | None = None,
) -> dict[str, Any]:
    """The `atls-compatibility-v1` envelope for one page.

    Emitted by `pull-md` so the caller knows what kind of document it received
    *before* editing it, and by `push-md --dry-run` so the same question is asked
    again against fresh remote state. The two are deliberately the same shape:
    a pull-time answer is a forecast, and a forecast that cannot be re-checked
    against reality is how stale approvals get reused.

    `document_path` is the file this assessment is about. It is what turns the
    next steps from advice into commands, and omitting it costs the caller those
    commands rather than handing them one with a hole in it.
    """

    assessment_options = cfxmark.ConversionOptions(profile=PROFILE)
    if base_artifact is None:
        report = assess_markdown_compatibility(storage, options=assessment_options)
    else:
        if getattr(base_artifact, "source_storage_sha256", None) != _sha256(storage):
            raise ValueError("precomputed Markdown artifact does not belong to the assessed storage")
        try:
            candidate = cfxmark.to_cfx_artifact(base_artifact.markdown, options=assessment_options).xhtml
        except Exception as error:  # noqa: BLE001 - the ordinary assessor uses the same fail-closed boundary
            report = cfxmark.CompatibilityReport(conversion_error=f"{type(error).__name__}: {error}")
        else:
            comparison = cfxmark.assess_candidate(storage, candidate)
            report = cfxmark.CompatibilityReport(
                judgements=comparison.judgements,
                candidate_markdown=base_artifact.markdown,
                candidate_storage=candidate,
            )
    status = STATUS_BY_CLASSIFICATION[report.classification]
    findings = [
        {
            "code": row.code,
            # The shared name for the same fact. The comparator's key is what it
            # measured under and the canonical code is what a person can act on;
            # dropping either breaks one of the two readers.
            "canonical_code": canonical_code(row.code),
            "title": title_for(row.code),
            "verdict": row.verdict,
            "count": row.count,
            "direction": row.direction,
            "semantic_paths": list(row.semantic_paths),
        }
        for row in report.findings()
        # `equivalent` findings are differences we have already explained away.
        # Listing them would bury the ones that need a decision.
        if row.verdict != "equivalent"
    ]

    # §8.2.1's second axis. The grade above says what Markdown alone would lose; this
    # says whether the managed publish path is *proven* to preserve it. Only ever
    # consulted for `xhtml_required`, because the other grades are already decided --
    # `migration_required` has named losses to approve and `converter_fix_required` is our
    # defect, and neither is a question about splicing remote structure back in.
    preservation = (
        preservation_for(
            findings,
            storage,
            converter=f"cfxmark {cfxmark.__version__}",
            profile=PROFILE,
            base_artifact=base_artifact,
        )
        if status.status == "xhtml_required"
        else None
    )

    # §8.2.1's second axis for the *identity* grade. Looked up rather than hardcoded,
    # and looked up through the module so a test can replace the registry and see the
    # version check actually decide something. `markdown_identity_bound` means "the
    # publish carries identity forward", and §8.2 permits the grade only where that
    # carry is registered for this converter and profile -- so this is the difference
    # between a name that promises a check and a name backed by one.
    identity = (
        preservation_module.identity_preservation_for(f"cfxmark {cfxmark.__version__}", PROFILE)
        if status.status == "markdown_identity_bound"
        else None
    )
    # And it must cover *this page*, not merely this build. R4-pre P1: the version check alone
    # granted a canonical write to every identity-bound page, including identity structures the
    # carry was never measured against.
    if identity is not None and not identity.covers(findings, storage):
        identity = None

    # Computed once. Both axes of §8.2.1 collapse into this single answer, and it is
    # the answer every consumer reads -- the envelope's field, the actions, and the pull
    # policy that decides whether a file appears on disk.
    #
    # The policy row is the floor and a registered capability is what lifts it. Written
    # as one expression rather than as a row somebody edits, because the row saying
    # `True` on its own is exactly what §8.2 forbids -- and is what it said before.
    canonical_write_permitted = status.canonical_write_permitted or preservation is not None or identity is not None
    effective_status = (
        replace(
            status,
            workflow_decision_required=False,
            recommended_workflow="markdown",
            summary=(
                "This page may be edited through managed Markdown because a registered preservation capability "
                "keeps the remote-only structure intact. Edit only outside the protected structures listed below."
            ),
        )
        if preservation is not None
        else _identity_not_proven(status)
        if status.status == "markdown_identity_bound" and identity is None
        else status
    )

    severity = severity_for(effective_status.status)
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "page_id": page_id,
        "classification": report.classification,
        "status": status.status,
        # Two fields an agent cannot miss, at the top of the envelope. Everything
        # below was already true; it was three levels down, and a caller that
        # does not descend publishes as if the page were plain.
        "attention_required": severity != "none",
        "attention_reason": (
            "protected_remote_structures"
            if preservation is not None
            # Named apart from the bare grade. `markdown_identity_bound` on its own reads
            # as "identity is handled"; this page is the case where it is not, and the
            # caller needs the difference to be visible without re-deriving it.
            else "identity_carry_not_proven_for_this_page"
            if status.status == "markdown_identity_bound" and identity is None
            else effective_status.status
            if severity != "none"
            else None
        ),
        "severity": severity,
        "summary": effective_status.summary,
        # Whether the caller has to choose *how to manage this page*. Not whether
        # a particular edit's losses need approving -- that is asked at publish
        # time, against the candidate, and is reported as
        # `publish_consent_required`.
        "workflow_decision_required": effective_status.workflow_decision_required,
        "recommended_workflow": effective_status.recommended_workflow,
        # §8.2's second column, in the envelope rather than only in the code that
        # enforces it. A caller that pulled and got no file needs to be able to
        # tell "the grade forbids it" from "the write failed", and those are
        # different situations with different next steps.
        "canonical_write_permitted": canonical_write_permitted,
        # The second axis, always present so a caller never has to infer its absence.
        # `null` for an `xhtml_required` page means the storage workflow, and for any
        # other grade it means the question does not apply.
        "preservation_capability": preservation.name if preservation is not None else None,
        # The identity axis, reported the same way and for the same reason: a caller that
        # may write this file should be able to see what the permission rests on.
        "identity_preservation_capability": identity.name if identity is not None else None,
        # What is protected remotely and must not be edited in Markdown. Said here
        # because a file that may be edited in most respects and not in one is worse than
        # a file that may not be edited at all, unless the exception is stated.
        "protected_remote_structures": list(preservation.protects) if preservation is not None else [],
        "requires_identity_carry": report.requires_identity_carry,
        "findings": findings,
        "candidate_sha256": _sha256(report.candidate_storage),
        "converter": f"cfxmark {cfxmark.__version__}",
        "profile": PROFILE,
        "next_actions": _next_actions(
            page_id,
            effective_status,
            report,
            document_path=document_path,
            storage_path=storage_path_for(document_path),
            canonical_write_permitted=canonical_write_permitted,
        ),
    }
    if report.conversion_error is not None:
        payload["conversion_error"] = report.conversion_error
    return payload


def candidate_loss(remote_storage: str, candidate_storage: str) -> dict[str, Any]:
    """What this candidate actually drops, as opposed to what the page might cost.

    The classification is a forecast: it asks what a from-scratch regeneration
    would lose. No publish path does that -- they splice the untouched parts of
    the remote back in -- so a page classified `xhtml_required` can publish a
    paragraph edit and lose nothing. Deriving "does the author need to approve
    this" from the forecast asks them to approve losses that are not going to
    happen, which is the same over-blocking this design set out to remove, moved
    from the gate into the guidance.

    So this asks the narrower question, on the document about to be written.

    It cannot be a plain diff of remote against candidate: an author's own edit is
    a difference too, and a first attempt at that reported every edited paragraph
    as an unexplained loss. Instead each loss-bearing construct the remote holds
    is matched to its counterpart in the candidate by content, and reported only
    when the construct survives and the thing that made it a loss does not.
    """

    from atlassian_skills.confluence.identity_gate import (
        MACRO_ELEMENT,
        TRACKED,
        find_dropped_attributes,
        find_rebound_attributes,
    )

    named: list[dict[str, Any]] = []
    for (element, attribute), code in NAMED_LOSS_ATTRIBUTES.items():
        for loss in find_dropped_attributes(
            remote_storage, candidate_storage, element=element, attributes=(attribute,)
        ):
            named.append({"code": code, "attribute": loss.attribute, "count": loss.detached})

    identity = [
        {"attribute": loss.attribute, "count": loss.detached, "ambiguous": loss.ambiguous, "kind": "dropped"}
        for loss in find_dropped_attributes(
            remote_storage, candidate_storage, element=MACRO_ELEMENT, attributes=TRACKED
        )
    ]
    # A swapped id is not a kept id. Only the drop was being looked for, so a
    # candidate replacing `ac:macro-id="A"` with `"B"` on the same macro reported
    # no loss at all -- the attribute is present, so nothing counted as detached.
    # The server treats an id it does not recognise as a new macro, so the
    # comments detach exactly as they would on a drop.
    identity += [
        {"attribute": loss.attribute, "count": loss.detached, "ambiguous": loss.ambiguous, "kind": "rebound"}
        for loss in find_rebound_attributes(
            remote_storage, candidate_storage, element=MACRO_ELEMENT, attributes=TRACKED
        )
    ]
    presentation = _presentation_canonicalizations(remote_storage, candidate_storage)
    return {
        "named_losses": named,
        "identity": identity,
        # Whether *this* publish needs the author to agree to something.
        #
        # R5-3: this counted only named losses, so a presentation change reported `false`
        # while the push refused without a fingerprint -- and the preflight exposes this value
        # as `publish_consent_required`, which the Skill tells agents to branch on. A public
        # contract disagreeing with itself is worse than a strict one: an agent that trusts the
        # dry run publishes nothing and cannot tell why.
        #
        # So it is whatever the gate does. The comment that used to sit here argued the
        # opposite -- that the editor converges `<p/>` anyway, so nobody need be asked. R4-pre
        # rejected that inference and the gate was changed; this field had not caught up.
        "requires_user_approval": bool(named) or presentation > 0,
        # Disclosure, kept beside the decision rather than instead of it. `full_migration`
        # rewrites every untouched `<p/>` on the page from an edit made elsewhere, so the
        # author's readers see different spacing. `change_kind: presentation` in the migration
        # report is how a caller asks the right question about it.
        "first_publish_changes_presentation": presentation > 0,
        "affected_occurrences": presentation,
    }


def _presentation_canonicalizations(remote_storage: str, candidate_storage: str) -> int:
    """How many places this candidate hands the platform its own canonical form.

    Counted from the registry rather than by looking for `<p><br/></p>` here, so
    the scope stays in one place. cfxmark decides *which* occurrences qualify --
    between ordinary body blocks, and nothing in a list item, a table cell, a
    blockquote, beside or inside a macro, leading or trailing -- and this only
    reports the number. A local pattern match would drift from that scope the first
    time either side moved, and drift towards over-reporting: the corpus's common
    case is the leading empty paragraph, which is a real loss and must not be
    counted here.
    """

    report = assess_candidate(remote_storage, candidate_storage)
    return sum(row.count for row in report.findings() if row.verdict == PLATFORM_EDITOR_CANONICALIZATION)


def compatibility_digest(page_id: str, storage: str) -> dict[str, Any]:
    """The assessment reduced to what may travel in an error envelope.

    Error contexts are logged and displayed, and this project holds a deny-by-
    default rule that no leaf value crosses that boundary. The full payload
    carries semantic paths, a summary sentence and a candidate hash, none of
    which belong there.

    What survives is what a caller needs in order to act: the classification, and
    which finding codes occurred how many times. A code is a name we chose; a
    count is an integer. Neither reveals what the page says.
    """

    payload = compatibility_payload(page_id, storage)
    return {
        "status": payload["status"],
        "attention_required": payload["attention_required"],
        "workflow_decision_required": payload["workflow_decision_required"],
        "requires_identity_carry": payload["requires_identity_carry"],
        "recommended_workflow": payload["recommended_workflow"],
        "findings": [
            {"code": finding["code"], "count": finding["count"]}
            for finding in payload["findings"]  # type: ignore[union-attr]
        ],
    }


__all__ = [
    "PROFILE",
    "SCHEMA",
    "storage_path_for",
    "STATUS_BY_CLASSIFICATION",
    "WorkflowStatus",
    "candidate_loss",
    # Re-exported so a consumer inside atls reaches the shape through this module,
    # the way every other compatibility question already does. The definition stays
    # in cfxmark: the shape belongs to whoever measured the platform, and a second
    # copy of "between ordinary body blocks" is the thing this release keeps
    # paying for.
    "canonicalization_sites",
    "compatibility_digest",
    "compatibility_payload",
]
