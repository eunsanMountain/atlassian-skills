"""Can this description be managed as Markdown, and on what evidence?

Five answers, and the discipline is that each names what a person must DO, not
how bad the news is:

    markdown_ready           edit as Markdown and publish
    markdown_identity_bound  the same, and the publish proves identity carried
    migration_required       named losses; show them and ask
    converter_fix_required   our gap, not the author's to approve
    wiki_required            use the exact wiki workflow

The judgement is a round trip, not a shape test. Jira wiki in, Markdown out,
Jira wiki back: if the third differs from the first, publishing that Markdown
would change the issue whether or not anything was "lost". Measured across a
synthetic corpus this caught five constructs that reported zero warnings and
zero losses, including the worst one -- a body written in Markdown, stored as
wiki, where `##` is a numbered-list marker and the headings quietly become list
items.

**`markdown_identity_bound` is assigned here, and was not before.**

The grade means "editable, and publishing verifies that identity was carried",
so it may only be given where that verification exists. It now does:
`description_push.assess_candidate` compares identity VALUES and their
multiplicity between the bound base and the candidate, against an issue read
freshly in the same call -- so a candidate that keeps one of two links, or swaps
one attachment's filename for another, is refused rather than published.

What withheld the grade before was that the three identity kinds were treated as
one. They do not behave alike. Measured through the real converter:

    smart link           [text|url]              round trips exactly
    attachment           !x.png!, !x.png|width=  round trips exactly
    user mention         [~alice]                DELETED, and reported as a loss

So the refusal is narrowed to the kind that is actually lost. A mention cannot
be republished at all -- the name it renders to would have to be looked up again
and the lookup can return the wrong person or nobody -- and that is a different
situation from a link, which comes back byte for byte.

`!x.png|thumbnail!` comes back as `!x.png|thumbnail=true!`, which is the same
image and not the same bytes. Nothing here special-cases it: the round trip
already refuses that body, which is the point of grading on a round trip rather
than on a list of kinds.

Naming the grade without the mechanism would be the failure this project has
already made once elsewhere -- a name promising a check nothing performs. The
mechanism is the precondition, not the name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MARKDOWN_READY = "markdown_ready"
MARKDOWN_IDENTITY_BOUND = "markdown_identity_bound"
MIGRATION_REQUIRED = "migration_required"
CONVERTER_FIX_REQUIRED = "converter_fix_required"
WIKI_REQUIRED = "wiki_required"

#: Constructs whose identity lives on the Jira side and cannot be rebuilt from
#: the Markdown alone. A user mention is the clearest: `[~alice]` renders as a
#: name, and regenerating it from the rendered name would need a lookup that can
#: return the wrong person or nobody.
_IDENTITY_BEARING = (
    (re.compile(r"\[~[^\]]+\]"), "user_mention"),
    (re.compile(r"\[[^\]|]*\|[^\]]*\]"), "smart_link"),
    (re.compile(r"!\S+\.(?:png|jpe?g|gif|svg|webp|bmp)[^!]*!", re.IGNORECASE), "attachment_reference"),
)

#: The kinds the round trip does not bring back, so no publish can prove they
#: were carried. Measured through the real converter, not assumed -- and the
#: measurement is the whole reason this is a subset rather than all three kinds.
#:
#: Membership is about DELETION, not about being hard. An attachment reference
#: written `!x.png|thumbnail!` comes back `!x.png|thumbnail=true!` and would fail
#: a value comparison, but the round trip refuses that body first; adding
#: `attachment_reference` here to cover it would also refuse every attachment
#: that does come back intact.
_IDENTITY_NOT_CARRIED = frozenset({"user_mention"})

#: Grades whose contract is "you may edit this file and publish it back".
_EDITABLE = frozenset({MARKDOWN_READY, MARKDOWN_IDENTITY_BOUND})


#: How many losses a serialized grade names. Matches the ten the Confluence ownership
#: summary shows, for the same reason: enough to see what kind of thing is wrong, bounded
#: so that a long description cannot turn a refusal into a copy of itself.
MAX_REPORTED_LOSSES = 10


@dataclass(frozen=True)
class DescriptionGrade:
    """The grade, and the evidence that produced it."""

    status: str
    #: Why, in the vocabulary a caller can branch on. Never free text.
    reasons: tuple[str, ...]
    #: Identity constructs found, by kind. Present even when the grade is not
    #: about them, because a reader deciding whether to convert wants to know.
    identity: tuple[str, ...]
    losses: tuple[str, ...]
    #: The first place a round trip diverged, as (stored, would_write). The line
    #: is what a person looks at; a byte offset is precise and unusable.
    first_difference: tuple[str, str] | None
    #: Which of the found kinds are the ones that blocked this. `identity` lists
    #: everything in the body, so on its own it cannot say which construct is the
    #: problem -- and "your description has a link and a mention in it" does not
    #: tell anybody that the mention is what they have to go and deal with.
    identity_not_carried: tuple[str, ...] = ()

    @property
    def editable_as_markdown(self) -> bool:
        return self.status in _EDITABLE

    def to_dict(self) -> dict[str, Any]:
        """The serialized grade, with the loss list capped.

        This dict reaches a CLI JSON envelope: `pull_md` attaches the grade to the
        refusal it raises when a description cannot be managed as Markdown. Each loss
        names the construct it is about -- `user mention [~alice] dropped` -- so a
        description with one per paragraph produced one envelope entry per paragraph.
        Measured on a 200-mention description: 200 entries, an envelope 62% the size of
        the description itself, in the output of a command that had refused to write
        anything.

        Capped rather than dropped, and the same way the Confluence side caps its leaf
        identities: the first page is what a reader needs to see the kind of thing that
        is wrong, `losses_total` says how many there were so the page is never mistaken
        for the whole, and `self.losses` keeps every one for a caller holding the object.
        """

        return {
            "status": self.status,
            "reasons": list(self.reasons),
            "identity": list(self.identity),
            "identity_not_carried": list(self.identity_not_carried),
            "losses": list(self.losses[:MAX_REPORTED_LOSSES]),
            "losses_total": len(self.losses),
            "first_difference": (
                {"stored": self.first_difference[0], "would_write": self.first_difference[1]}
                if self.first_difference
                else None
            ),
            "editable_as_markdown": self.editable_as_markdown,
        }


def find_identity(wiki: str) -> tuple[str, ...]:
    """Identity-bearing constructs in the stored markup, by kind.

    Kinds only. Useful for grading -- "does this description have identity in
    it at all" -- and useless for proving a candidate, where the question is
    whether THESE mentions survived. Use `identity_values` for that.
    """

    return tuple(sorted({kind for pattern, kind in _IDENTITY_BEARING if pattern.search(wiki)}))


def identity_values(wiki: str) -> dict[str, tuple[str, ...]]:
    """Every identity-bearing construct, by kind, as its actual text.

    Values and multiplicity, not a set of kinds. A candidate that keeps one of
    two mentions still "has mentions"; a candidate that swaps `[~alice]` for
    `[~bob]` still "has mentions". Both detach a person from an issue, and a
    comparison of kinds calls both of them unchanged.

    Sorted rather than positional: moving a mention within a description is not
    losing it, and reporting a move as a loss refuses an ordinary edit.
    """

    found: dict[str, tuple[str, ...]] = {}
    for pattern, kind in _IDENTITY_BEARING:
        matches = tuple(sorted(match.group(0) for match in pattern.finditer(wiki)))
        if matches:
            found[kind] = matches
    return found


#: The filename inside an attachment reference, which is all a reference names.
#: `!diagram.png|width=400!` says `diagram.png` and nothing about which
#: attachment carries it -- and Jira accepts two attachments under one name.
_ATTACHMENT_FILENAME = re.compile(
    r"!(?P<name>[^!|\s]+\.(?:png|jpe?g|gif|svg|webp|bmp))(?:\|[^!]*)?!",
    re.IGNORECASE,
)


def attachment_filenames(wiki: str) -> tuple[str, ...]:
    """Every filename the body's attachment references name, in order, with
    repeats. Repeats are kept because two references to one file are two things
    a candidate has to still have."""

    return tuple(match.group("name") for match in _ATTACHMENT_FILENAME.finditer(wiki))


def grade_description(
    wiki: str,
    *,
    read_report: Any,
    conversion_failed: bool = False,
) -> DescriptionGrade:
    """Grade from the round trip that was already run, not from a second one.

    Takes the read report rather than recomputing: two round trips over the same
    body can disagree, and then the grade and the warning a caller sees are
    answering different questions about the same description.
    """

    identity = find_identity(wiki)
    losses = tuple(getattr(read_report, "losses", ()) or ())
    difference = getattr(read_report, "first_difference", None)
    reasons: list[str] = []

    if conversion_failed:
        # Our defect, and the author cannot consent their way out of it.
        return DescriptionGrade(CONVERTER_FIX_REQUIRED, ("conversion_failed",), identity, losses, difference)

    if not getattr(read_report, "content_complete", False):
        reasons.append("content_incomplete")

    uncarried = tuple(kind for kind in identity if kind in _IDENTITY_NOT_CARRIED)
    if uncarried:
        # Ahead of the loss gate on purpose. A mention also arrives as a named
        # loss, and `migration_required` means "show the author and let them
        # accept it" -- which would offer a choice that does not exist, because
        # there is no way to put the mention back.
        reasons.append("identity_not_carried")
        return DescriptionGrade(WIKI_REQUIRED, tuple(reasons), identity, losses, difference, uncarried)

    if not getattr(read_report, "write_back_safe", False):
        reasons.append("write_back_would_change_the_body")
        return DescriptionGrade(WIKI_REQUIRED, tuple(reasons), identity, losses, difference)

    if losses:
        reasons.append("named_losses")
        return DescriptionGrade(MIGRATION_REQUIRED, tuple(reasons), identity, losses, difference)

    if reasons:
        return DescriptionGrade(WIKI_REQUIRED, tuple(reasons), identity, losses, difference)

    if identity:
        # Editable, and the publish has something extra to prove. The grade is
        # the instruction to `assess_candidate`: these values, this many times,
        # against an issue read in the same call.
        return DescriptionGrade(MARKDOWN_IDENTITY_BOUND, (), identity, losses, difference)

    return DescriptionGrade(MARKDOWN_READY, (), identity, losses, difference)


__all__ = [
    "CONVERTER_FIX_REQUIRED",
    "MARKDOWN_IDENTITY_BOUND",
    "MARKDOWN_READY",
    "MAX_REPORTED_LOSSES",
    "MIGRATION_REQUIRED",
    "WIKI_REQUIRED",
    "DescriptionGrade",
    "find_identity",
    "grade_description",
    "identity_values",
]
