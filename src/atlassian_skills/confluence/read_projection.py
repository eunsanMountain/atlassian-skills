r"""Whether the Markdown a reader was handed still contains the page.

`page get --body-repr=md` is a projection for reading, not an input for
publishing, and mostly it is faithful. Sometimes it is not: a macro inside a
table cell renders as `cfx:expand` and its body is simply not there. An agent
summarising from that output is confidently wrong, and nothing in the command's
exit code, its status fields or its diagnostics says so.

Three questions, deliberately separate, because collapsing them makes the answer
useless. Measured over the 55-page corpus:

    presentation lost   a cell background, a column width
                        text intact; a warning at most
    structure lost      an element replaced by a placeholder that stood in for
                        nothing a reader could read -- 8 pages
    content lost        the element carried text and that text is not in the
                        output at all -- 13 pages

Together that is 38% of the corpus. Collapsed into one "lossy" flag, better than
one page in three would tell the reader to go and fetch the storage, and nobody
would. The thirteen that genuinely lose text are worth interrupting for precisely
because they are thirteen and not twenty-one.

The verdict is structural, and it used to be a word search. That is the whole
correction: searching the output for the element's words could only ever fail to
find them, so a page was called complete whenever the search happened to succeed
for the wrong reason. Three ways it did:

    <expand>중요 경고</expand>      both words are two characters, and words
                                    shorter than four were discarded as noise
    <expand>1234 5678</expand>      digits were discarded too
    <expand>critical warning</expand>
                                    with `critical warning` also in a paragraph
                                    elsewhere, the search found *that* one

All three render as `[cfx:expand]` -- the body is gone in every case -- and all
three were reported as complete.

The search cannot succeed honestly, because on this projection cfxmark renders an
opaque element as its label and nothing else: `InlineOpaque` becomes `[label]`,
`OpaqueBlock` becomes `> [Unsupported Confluence content]`. There is no path by
which the body reaches the output, so the question worth asking is the one that
can be answered from the source alone: did this element carry text a reader would
have seen? If it did, that text is gone.

Measured over the 55-page corpus, this moves one page from complete to
incomplete -- op-09, whose opaque table held the word `Recall` that also appeared
in a heading. The pages that remain shape-only are the ones that genuinely carry
no text: `@user` mentions, attachment links, date links, all of which keep their
label in an attribute.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

_TAG = re.compile(r"<[^>]+>")
_CDATA = re.compile(r"<!\[CDATA\[|\]\]>")


def reader_text(fragment: str) -> str:
    """The text a reader would have seen inside this storage fragment.

    Everything between the angle brackets goes, which drops attribute values with
    it: `ac:name="expand"` is how the element is spelled, not something anyone
    reads. What is left is element text, and that includes the parts a reader
    does see but might not think of as body -- an expand's title parameter, the
    contents of a `CDATA` code block.

    No filtering beyond that. An earlier version kept only words of four
    characters or more and dropped anything numeric, which silently excused
    `중요 경고` and `1234 5678` from the check.
    """

    return html.unescape(_TAG.sub(" ", _CDATA.sub(" ", fragment))).strip()


@dataclass(frozen=True)
class Omission:
    """One element the projection stood something else in for."""

    code: str
    label: str
    semantic_path: str
    #: True when the element's words are absent from the output, not merely its
    #: shape. This is the one that should send a reader to `view` or `storage`.
    content_lost: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label,
            "semantic_path": self.semantic_path,
            "content_lost": self.content_lost,
        }


@dataclass(frozen=True)
class ProjectionReport:
    """What this Markdown does and does not still hold."""

    content_complete: bool
    structure_complete: bool
    presentation_complete: bool
    omissions: tuple[Omission, ...]

    @property
    def attention_required(self) -> bool:
        return not (self.content_complete and self.structure_complete)

    @property
    def reason(self) -> str | None:
        if not self.content_complete:
            return "content_incomplete"
        if not self.structure_complete:
            return "structure_incomplete"
        return None

    def to_dict(self, page_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "content_complete": self.content_complete,
            "structure_complete": self.structure_complete,
            "presentation_complete": self.presentation_complete,
            "attention_required": self.attention_required,
            "attention_reason": self.reason,
            "omissions": [item.to_dict() for item in self.omissions],
        }
        if self.attention_required:
            payload["next_actions"] = _next_actions(page_id, content_lost=not self.content_complete)
        return payload


def _next_actions(page_id: str, *, content_lost: bool) -> list[dict[str, Any]]:
    """Where to read the part that is not here.

    `view` first: it is what the page looks like to a person, which is what a
    summary is about. `storage` second, for a caller that needs the exact
    markup. Both are reads, so neither needs anyone's approval -- and asking
    would be the thing that stops an agent doing it.
    """

    actions = [
        {
            "label": "read the page as the server renders it",
            "argv": ["confluence", "page", "get", page_id, "--body-repr=view", "--format=raw"],
            "requires_user_approval": False,
        }
    ]
    if content_lost:
        actions.append(
            {
                "label": "read the exact storage this page holds",
                "argv": ["confluence", "page", "get", page_id, "--body-repr=storage", "--format=raw"],
                "requires_user_approval": False,
            }
        )
    return actions


def _opaque_nodes(document: Any) -> list[Any]:
    """Every element the projection replaced with a stand-in.

    Walked over the document rather than matched in the text: `cfx:expand` in
    the output could equally be something an author typed, and a check that
    cannot tell those apart is one that gets switched off.
    """

    seen: list[Any] = []
    stack = [document]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        if "Opaque" in type(node).__name__:
            seen.append(node)
        for field in ("children", "items", "rows", "cells", "header", "body"):
            value = getattr(node, field, None)
            if isinstance(value, (list, tuple)):
                stack.extend(item for item in value if hasattr(item, "__dataclass_fields__"))
            elif hasattr(value, "__dataclass_fields__"):
                stack.append(value)
    return seen


def assess_read_projection(document: Any) -> ProjectionReport:
    """Ask, of each element that was stood in for, whether it carried any text.

    The document is enough to answer that, and the Markdown cannot help: this
    projection renders an opaque element as its label, so a body that existed is
    a body that is gone. Taking the output as evidence is what let a page whose
    words happened to appear elsewhere be reported as complete.
    """

    omissions: list[Omission] = []
    for node in _opaque_nodes(document):
        path = getattr(node, "source_path", ()) or ()
        lost = bool(reader_text(getattr(node, "raw_xml", "") or ""))
        omissions.append(
            Omission(
                code="element_body_omitted" if lost else "element_shape_replaced",
                label=str(getattr(node, "label", "") or "element"),
                semantic_path="/".join(str(part) for part in path),
                content_lost=lost,
            )
        )

    return ProjectionReport(
        content_complete=not any(item.content_lost for item in omissions),
        structure_complete=not omissions,
        # Presentation is reported by the converter itself and is not a reason to
        # go and read anything: the text is all here.
        presentation_complete=True,
        omissions=tuple(omissions),
    )


__all__ = ["Omission", "ProjectionReport", "assess_read_projection", "reader_text"]
