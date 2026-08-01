"""What atls knows about the issue a description file was pulled from.

A Jira schema, not a renamed Confluence one. The two products bind on different
things and pretending otherwise is how one record starts lying about the other:

    Confluence   page id + version number
    Jira         issue id AND key + `updated` + source sha256 + attachment set

Jira has no version counter. `updated` is the nearest thing and it is not
enough on its own -- it is a timestamp, it moves for reasons that have nothing
to do with the description, and two writes inside its resolution are
indistinguishable. So the binding carries the hash of the description itself,
and staleness is decided on all of it together.

The hash is over the UTF-8 bytes of the description STRING as Jira returned it,
not over the HTTP JSON that carried it. The envelope has fields that change
without the description changing, and a binding that moves when nothing moved
teaches its reader to ignore it.

Issue id AND key, because a key can be moved to another project while the id
stays put. Binding on the key alone would let a file follow a rename onto an
issue it was never pulled from; binding on the id alone would leave a human
unable to tell which issue a stray file belongs to.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCHEMA = "atls-jira-description-v1"

#: Sorts beside the file it describes and cannot be mistaken for the document.
#: Not a dotfile: a hidden file is one a person copies without noticing, and
#: then wonders why the stale check stopped working.
SUFFIX = ".atls.json"


def binding_path(description_path: Path) -> Path:
    return description_path.with_name(description_path.name + SUFFIX)


def source_sha256(description: str) -> str:
    """Hash of the description string's UTF-8 bytes. Not of the JSON envelope."""

    return f"sha256:{hashlib.sha256(description.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class DescriptionBinding:
    """The remote state a local description file was taken from."""

    issue_id: str
    issue_key: str
    site: str
    #: Jira's own `updated`, verbatim. Compared as an opaque string: parsing it
    #: into a datetime invites a comparison that says "close enough".
    remote_updated: str
    source_sha256: str
    #: Which representation publishes from this directory. `wiki` is the exact
    #: one and always available; `md` is only reachable for a description whose
    #: conversion was proven, and naming it here stops the two racing.
    authority: str = "wiki"
    #: The description as pulled, before any local edit -- the base a three-way
    #: merge needs. A hash can say THAT it changed, never what it said.
    base_wiki: str = ""
    #: The Markdown as pulled, for a file being edited as Markdown. Kept beside
    #: `base_wiki` rather than instead of it: a merge on the Markdown side needs
    #: the Markdown base, and republishing needs the wiki the file came from.
    #: One of the two is empty depending on `authority`, and which one is empty
    #: is derivable from it rather than being a second place to disagree.
    base_markdown: str = ""
    #: The grade the description held when pulled, so a later push can notice
    #: that the remote turned into something Markdown may no longer manage.
    grade: str = ""
    #: Attachment identity as pulled: id, filename and hash together. Filename
    #: alone is not identity on Jira -- it accepts two attachments under one
    #: name -- so a set keyed on filename would silently conflate them.
    attachments: tuple[dict[str, str], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {"schema": SCHEMA, **asdict(self)}

    @classmethod
    def from_dict(cls, payload: dict) -> DescriptionBinding:
        known = {key: value for key, value in payload.items() if key != "schema"}
        attachments = tuple(dict(item) for item in known.pop("attachments", ()) or ())
        return cls(attachments=attachments, **known)


def write_binding(description_path: Path, binding: DescriptionBinding) -> Path:
    path = binding_path(description_path)
    path.write_text(json.dumps(binding.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_binding(description_path: Path) -> DescriptionBinding | None:
    """The binding beside this file, or None when there is not one.

    None rather than a raised error: a missing binding narrows what is possible
    (no stale check, no merge) without making the file unusable, and the caller
    is the one that knows which of those it needed.
    """

    path = binding_path(description_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return None
    try:
        return DescriptionBinding.from_dict(payload)
    except TypeError:
        # A binding written by a version that knew different fields. Treated as
        # absent rather than guessed at, because a partially understood binding
        # is what a stale check would then trust.
        return None


__all__ = [
    "SCHEMA",
    "SUFFIX",
    "DescriptionBinding",
    "binding_path",
    "read_binding",
    "source_sha256",
    "write_binding",
]
