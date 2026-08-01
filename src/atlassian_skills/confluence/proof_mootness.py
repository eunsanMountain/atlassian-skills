"""When the ownership proof's question has no answer worth waiting for.

The ownership proof asks which opaque remote leaf a local edit claims, and it
asks because a publish splices the untouched parts of the remote back in: get the
attribution wrong and an edit lands on a region nobody touched. Refusing when it
cannot tell is correct.

It is also, measured, the thing that stops edits which could not possibly have
gone wrong. On one corpus of twenty pages it refused five that dropped nothing at
all -- and the trigger was edit size, not edit risk.

The narrowing is not "trust the proof less". It is to notice that on some pages
the question it asks does not need answering, and to *check* that rather than
assume it:

    the candidate drops no named loss and no identity
        -- checked first because it is the most specific thing that can be
           wrong with this particular publish

    the remote holds nothing Markdown cannot express
        -- so there is nothing that must be carried over from it

    the document about to be published is what a from-scratch render of the
    edited Markdown produces
        -- so no remote fragment was carried in and no edit landed elsewhere;
           whatever the source map believed, it changed nothing

All three, or the refusal stands. The last is the load-bearing test, because it
looks at the document that will be written rather than at the process that wrote
it: a source map can be as confused as it likes about a candidate that turned out
identical to the plain rendering.

The second is what makes the last one safe, and it is not a formality. Measured:
on a page with a list, the spliced candidate and a from-scratch render agree
exactly -- and publishing it unwraps the paragraph inside list items the author
never touched, which the server stores and renders differently. Without the
classification condition this gate would ship that.

Which is why, on the corpus this was built against, it opens on nothing. Every
measured over-block is a page whose publish really would alter untouched content,
so the proof was right and the over-blocking lives upstream, in a converter gap
that was deliberately deferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cfxmark
from cfxmark.compatibility import MARKDOWN_LOSSLESS, assess_candidate, assess_markdown_compatibility

from atlassian_skills.confluence.compatibility import PROFILE, candidate_loss


@dataclass(frozen=True)
class Mootness:
    """Whether the proof may be set aside here, and what decided it."""

    moot: bool
    #: Named so a refusal can say which condition held it closed, and so a run
    #: over a corpus can be counted by reason rather than by outcome.
    reason: str
    classification: str

    def to_dict(self) -> dict[str, Any]:
        return {"moot": self.moot, "reason": self.reason, "classification": self.classification}


def assess_proof_mootness(
    source_storage: str,
    candidate_storage: str,
    edited_markdown: str,
    *,
    options: Any,
) -> Mootness:
    """Ask whether attribution can matter for this publish. Default: it can.

    Every path out of the checks below that is not the last one keeps the
    refusal. An exception from the converter keeps it too -- a page we could not
    re-render is not a page we understand well enough to stop checking.
    """

    # First because it is the most specific thing that can be wrong with *this*
    # publish. Ordered after the classification it was unreachable -- every page
    # holding something droppable is already not lossless -- and "the page is not
    # lossless" is a worse account of a candidate that dropped a cell background
    # than saying so.
    try:
        loss = candidate_loss(source_storage, candidate_storage)
    except Exception:  # noqa: BLE001 - a document we cannot read is not an exempt one
        return Mootness(False, "candidate_unreadable", "")
    if loss["named_losses"] or loss["identity"]:
        return Mootness(False, "candidate_drops_something", "")

    try:
        report = assess_markdown_compatibility(source_storage, options=cfxmark.ConversionOptions(profile=PROFILE))
    except Exception:  # noqa: BLE001 - an unassessable page is not an exempt page
        return Mootness(False, "classification_unavailable", "")

    classification = report.classification
    if classification != MARKDOWN_LOSSLESS:
        # Includes every page holding a macro: measured, a macro is enough to
        # make a page not lossless, which is what keeps the macro-in-a-table-cell
        # specimen -- destroyed silently, and reported as no loss -- on the far
        # side of this gate.
        return Mootness(False, "page_is_not_markdown_lossless", classification)

    try:
        fresh = cfxmark.to_cfx_artifact(edited_markdown, options=options)
    except Exception:  # noqa: BLE001 - same rule: no re-render, no exemption
        return Mootness(False, "regeneration_unavailable", classification)

    agreement = assess_candidate(candidate_storage, fresh.xhtml)
    if any(row.verdict != "equivalent" for row in agreement.findings()):
        # The candidate says something a plain render of the same Markdown does
        # not. That difference came from the remote, through the splice, which is
        # exactly the machinery whose attribution is in doubt.
        return Mootness(False, "candidate_differs_from_regeneration", classification)

    return Mootness(True, "nothing_bound_to_the_source", classification)


__all__ = ["Mootness", "assess_proof_mootness"]
