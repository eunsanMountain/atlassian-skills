r"""What `issue get --body-repr=md` can and cannot promise about a Jira body.

Two questions, and the first version of this file answered them as one. That was
wrong in the direction that matters -- it told readers not to summarise from
output that was perfectly good.

    can I read this?      does the body's text reach the Markdown
    can I write it back?  would publishing this Markdown change what is stored

Measured over the fifteen shapes the harness puts on a live server, every one
keeps all of its text -- headings, panels, expands, macros in table cells, a
description written in Markdown. What varies is the other question: seven of the
fifteen come back as different wiki than they went in.

That corpus had no user mention in it, and a later measurement found the one
shape where reading really does lose something: `[~alice] please review` becomes
` please review`. The person is gone. So "not one word is lost", which this file
used to claim outright, was true of the fifteen and not of Jira -- the reason
the read verdict is computed rather than assumed. A mention now reaches this
module as a loss, and `content_complete` says no.

The clearest is not the dramatic one:

    h3. 개요        reads as    ### 개요        which is right
                    writes as   h2. 개요        which is a different heading

Nothing about reading that body is wrong. Writing it back silently promotes the
heading. Reporting it as `content_incomplete`, as this module first did, sends a
reader to fetch raw markup they did not need and tells them not to trust a
summary that was accurate.

So the round trip is reported as what it measures: write-back safety. The read
verdict is grounded separately, in the text of the parsed body -- the wiki source
cannot simply be tokenised, because `{panel:title=...}` would contribute the word
`panel` and every macro would look like content.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

import cfxmark

#: A body the caller deliberately asked only part of -- `--section`, a dropped
#: notice. Incomplete for a reason that has nothing to do with conversion.
REQUESTED_PROJECTION = "requested_projection"
#: The conversion did not carry the body's text across.
CONTENT_INCOMPLETE = "content_incomplete"
#: The text is all here, and publishing it back would still change the issue.
BODY_WOULD_CHANGE = "body_would_change"

_TRAILING = re.compile(r"[ \t]+$", re.MULTILINE)
_TOKEN = re.compile(r"\w+", re.UNICODE)
#: Markdown escapes what would otherwise be punctuation, so `frame_idx` is
#: written `frame\_idx` and the source spelling is not found.
_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+.!|~<>-])")


def comparable(wiki: str) -> str:
    """The body with the differences that are not the body removed.

    Blank lines and trailing spaces move whenever a renderer is involved, and
    comparing them called every faithful shape a change. This is the least
    normalisation that let the faithful shapes through, and no more -- anything
    further starts hiding real differences.

    Public because the write path needs the same line. `write_back_safe` is
    decided here on the normalised comparison, so a body admitted as manageable
    can still differ from what publishing it would store -- by exactly this much
    and no more. The publish uses this to say `whitespace_only_change` instead
    of reporting a converter defect that does not exist, which is what an exact
    comparison alone made it do.
    """

    return "\n".join(line for line in _TRAILING.sub("", wiki).strip().splitlines() if line.strip())


def _document_text(node: Any, into: list[str] | None = None) -> list[str]:
    """Every string the parsed body holds.

    Taken from the document rather than the wiki source because the source
    cannot be read as prose: `{panel:title=Important}` would contribute `panel`,
    `color` and `noformat` as though they were words somebody wrote.
    """

    collected = [] if into is None else into
    content = getattr(node, "content", None)
    if isinstance(content, str):
        collected.append(content)
    for field in ("children", "items", "rows", "cells", "header", "body"):
        value = getattr(node, field, None)
        if isinstance(value, (list, tuple)):
            for item in value:
                _document_text(item, collected)
        elif hasattr(value, "__dataclass_fields__"):
            _document_text(value, collected)
    return collected


def _missing_words(document: Any, markdown: str) -> list[str]:
    """Words the body holds that the Markdown does not.

    Counted rather than merely looked for. A body that repeats a term can lose a
    whole sentence while every word in it is still present somewhere else.
    """

    if document is None:
        return []
    source = Counter(_TOKEN.findall(" ".join(_document_text(document))))
    rendered = Counter(_TOKEN.findall(_MD_ESCAPE.sub(r"\1", markdown)))
    return sorted(word for word, count in source.items() if rendered[word] < count)


@dataclass(frozen=True)
class JiraReadReport:
    """What this Markdown is good for."""

    #: The body's text reached the Markdown.
    content_complete: bool
    #: Publishing this Markdown back would leave the stored body unchanged.
    write_back_safe: bool
    reason: str | None
    #: The first line that came back different, as `(stored, regenerated)`. The
    #: difference itself, because "it differs" leaves the caller to find it.
    first_difference: tuple[str, str] | None
    losses: tuple[str, ...]
    attachments: tuple[str, ...]
    missing_words: tuple[str, ...]

    @property
    def attention_required(self) -> bool:
        return not (self.content_complete and self.write_back_safe) or self.reason == REQUESTED_PROJECTION

    def to_dict(self, issue_key: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "content_complete": self.content_complete,
            "write_back_safe": self.write_back_safe,
            "attention_required": self.attention_required,
            "attention_reason": self.reason,
            "losses": list(self.losses),
            "attachments": list(self.attachments),
        }
        if self.missing_words:
            payload["missing_words"] = list(self.missing_words)
        if self.first_difference is not None:
            stored, regenerated = self.first_difference
            payload["first_difference"] = {"stored": stored, "would_write": regenerated}
        if self.attention_required:
            payload["next_actions"] = _next_actions(issue_key, self.reason)
        return payload


def _next_actions(issue_key: str, reason: str | None) -> list[dict[str, Any]]:
    """Where to go next, chosen by what is actually wrong.

    Reads, all of them, so none waits on anyone's approval -- asking is what
    would stop an agent doing it.
    """

    if reason == REQUESTED_PROJECTION:
        # Nothing was lost; the caller asked for a part. The way to see the rest
        # is to ask for the rest.
        return [
            {
                "label": "read the whole body rather than the requested section",
                "argv": ["jira", "issue", "get", issue_key, "--body-repr=md"],
                "requires_user_approval": False,
            }
        ]
    label = (
        "read the exact wiki markup, and edit that instead of this Markdown"
        if reason == BODY_WOULD_CHANGE
        else "read the exact wiki markup this issue stores"
    )
    return [
        {
            "label": label,
            "argv": ["jira", "issue", "get", issue_key, "--fields", "description", "--format=raw"],
            "requires_user_approval": False,
        }
    ]


def _first_difference(stored: str, regenerated: str) -> tuple[str, str] | None:
    left = comparable(stored).splitlines()
    right = comparable(regenerated).splitlines()
    for index in range(max(len(left), len(right))):
        a = left[index] if index < len(left) else ""
        b = right[index] if index < len(right) else ""
        if a != b:
            return (a, b)
    return None


def assess_jira_read(
    wiki: str,
    markdown: str,
    *,
    document: Any = None,
    losses: tuple[str, ...] = (),
    attachments: tuple[str, ...] = (),
    requested_projection: bool = False,
) -> JiraReadReport:
    """Judge the two questions separately, because they have different answers."""

    missing = tuple(_missing_words(document, markdown))
    content_complete = not missing and not losses

    if requested_projection:
        # Running the round trip here would compare a section against the whole
        # body and report a difference that is the caller's own instruction.
        return JiraReadReport(
            content_complete=content_complete,
            write_back_safe=False,
            reason=REQUESTED_PROJECTION,
            first_difference=None,
            losses=losses,
            attachments=attachments,
            missing_words=missing,
        )

    difference: tuple[str, str] | None = None
    write_back_safe = True
    if wiki.strip():
        try:
            regenerated = cfxmark.to_jira_wiki(markdown).jira_wiki or ""
        except Exception:  # noqa: BLE001 - a body that cannot be re-rendered is one we cannot vouch for
            write_back_safe = False
        else:
            difference = _first_difference(wiki, regenerated)
            write_back_safe = difference is None

    reason = None
    if not content_complete:
        reason = CONTENT_INCOMPLETE
    elif not write_back_safe:
        reason = BODY_WOULD_CHANGE

    return JiraReadReport(
        content_complete=content_complete,
        write_back_safe=write_back_safe,
        reason=reason,
        first_difference=difference,
        losses=losses,
        attachments=attachments,
        missing_words=missing,
    )


__all__ = [
    "BODY_WOULD_CHANGE",
    "CONTENT_INCOMPLETE",
    "REQUESTED_PROJECTION",
    "JiraReadReport",
    "assess_jira_read",
    "comparable",
]
