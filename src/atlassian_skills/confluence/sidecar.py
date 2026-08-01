"""The base snapshot a three-way merge needs, kept beside the managed file.

The managed manifest already records `base_md`, but only as a hash. A hash can
say *that* a file changed and never *what it said before*, and a three-way merge
needs the before-text of all three sides. So the base Markdown is written once,
at pull time, next to the file it is the base for.

This costs something, and the cost is worth naming rather than hiding. The
managed format's selling point is that one file is the whole document -- copy it,
move it, mail it, and it still works. A sidecar breaks that, so the contract is
narrowed rather than abandoned:

    sidecar present    pull, push, and three-way merge
    sidecar missing    pull and push, and the merge is reported unavailable

A caller who moved one file loses a capability. They do not lose data, and they
are told which capability, because a silent fall back to two-way merge would
overwrite the remote edit the merge existed to preserve.

Owned by atls alone. An earlier design shared it with a downstream workflow's
own sidecar, which put page version and hash in two places -- and two records of
the same fact disagree eventually.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "atls-sidecar-v1"

#: Chosen so the sidecar sorts next to its file and cannot be mistaken for the
#: document. `.atls.json` rather than a dotfile: a hidden file is one a user
#: copies without realising, and then wonders why the merge stopped working.
SUFFIX = ".atls.json"


#: The `authority` field of an `atls:managed` manifest line.
_MANAGED_AUTHORITY_RE = re.compile(r"<!--\s*atls:managed\s[^>]*?\bauthority=(?P<authority>[^\s>]+)")


def sidecar_path(managed_path: Path) -> Path:
    return managed_path.with_name(managed_path.name + SUFFIX)


@dataclass(frozen=True)
class Sidecar:
    """What atls knows about the page a managed file was pulled from."""

    page_id: str
    site: str
    remote_version: int
    remote_storage_sha256: str
    converter: str
    profile: str
    #: The Markdown as pulled, before any local edit. The whole reason this file
    #: exists.
    base_markdown: str
    #: Which representation is authoritative for publishing. A page whose losses
    #: cannot be classified moves to `xhtml`, and `push-md` must refuse rather
    #: than publish a Markdown rendering of a document Markdown cannot hold.
    authority: str = "markdown"
    #: The storage document as pulled, for a file being edited as XHTML. Same
    #: role as `base_markdown` and the same reason: without the before-text, an
    #: offline check cannot tell a macro whose id was dropped from one that
    #: never had one.
    base_storage: str = ""

    def to_json(self) -> bytes:
        payload = {
            "schema": SCHEMA,
            "page_id": self.page_id,
            "site": self.site,
            "remote_version": self.remote_version,
            "remote_storage_sha256": self.remote_storage_sha256,
            "converter": self.converter,
            "profile": self.profile,
            "authority": self.authority,
            "base_markdown": self.base_markdown,
            "base_storage": self.base_storage,
        }
        return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


class SidecarUnusable(Exception):
    """The sidecar is missing, unreadable, or describes a different page.

    One exception for all three because the caller's next move is the same in
    every case -- merge unavailable, say so -- and because guessing which of them
    happened is how a corrupted file gets treated as an absent one.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def read_sidecar(managed_path: Path, *, page_id: str) -> Sidecar:
    """Load the base snapshot for this file, or say exactly why it cannot be used.

    Refuses a sidecar belonging to another page. Managed files get copied between
    directories, and a base snapshot from the wrong page would merge one
    document's history into another -- a failure that produces a plausible
    document rather than an error.
    """

    return _load(managed_path, page_id=page_id, required="base_markdown")


def read_xhtml_sidecar(xhtml_path: Path, *, page_id: str) -> Sidecar:
    """The same record, for a file being edited as storage rather than Markdown.

    One schema and one loader, because two records of a page's version and hash
    disagree eventually. What differs is which before-text has to be there: the
    Markdown path cannot merge without `base_markdown`, and the storage path
    cannot tell a dropped macro id from an absent one without `base_storage`.
    """

    return _load(xhtml_path, page_id=page_id, required="base_storage")


def _load(path_of_document: Path, *, page_id: str, required: str) -> Sidecar:
    path = sidecar_path(path_of_document)
    if not path.exists():
        raise SidecarUnusable("sidecar_missing", str(path))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SidecarUnusable("sidecar_unreadable", str(error)) from error
    if not isinstance(payload, dict):
        raise SidecarUnusable("sidecar_unreadable", "not an object")
    if payload.get("schema") != SCHEMA:
        raise SidecarUnusable("sidecar_schema_mismatch", str(payload.get("schema")))
    if payload.get("page_id") != page_id:
        raise SidecarUnusable("sidecar_page_mismatch", str(payload.get("page_id")))
    # Non-empty, not merely present. Both pulls write both keys, so presence no
    # longer says which pull wrote the file, and an empty before-text would let a
    # Markdown sidecar pass the storage check and answer "nothing was dropped"
    # about a document it has never seen. The cost is that a page with an empty
    # body reports `sidecar_incomplete` -- a named refusal on a document that has
    # nothing to merge and nothing to lose.
    if not isinstance(payload.get(required), str) or not payload[required]:
        raise SidecarUnusable("sidecar_incomplete", required)
    return Sidecar(
        page_id=str(payload["page_id"]),
        site=str(payload.get("site", "")),
        remote_version=int(payload.get("remote_version", 0)),
        remote_storage_sha256=str(payload.get("remote_storage_sha256", "")),
        converter=str(payload.get("converter", "")),
        profile=str(payload.get("profile", "")),
        base_markdown=str(payload.get("base_markdown", "")),
        authority=str(payload.get("authority", "markdown")),
        base_storage=str(payload.get("base_storage", "")),
    )


def read_authority(document_path: Path) -> str | None:
    """Which representation may publish this page, or None if nothing says.

    Deliberately quiet: a missing or unreadable sidecar means no one declared an
    authority, which is not the same as declaring Markdown. Callers that need a
    base snapshot ask for one and get a named refusal; callers that only need to
    know whether they have been shut out should not be stopped by a file that was
    never written.
    """

    path = sidecar_path(document_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _manifest_authority(document_path)
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return _manifest_authority(document_path)
    authority = payload.get("authority")
    return authority if isinstance(authority, str) else _manifest_authority(document_path)


#: The manifest's own spelling, mapped to this module's. `md` and `markdown` are the
#: same answer in two vocabularies -- the manifest is terse because it is a comment
#: inside every managed file, and this module's word is what the workflow reads.
_AUTHORITY_FROM_MANIFEST = {"md": "markdown"}


def _manifest_authority(document_path: Path) -> str | None:
    """What the document says about itself, when no sidecar says anything.

    A managed Markdown file has carried `authority` in its v3 manifest since the
    manifest became the only required persistent metadata -- and once the pull stopped
    writing a sidecar by default, the sidecar's silence stopped meaning "nobody
    declared". Reading only the sidecar answered `None` for a file that states its
    authority in its first line, which reads as "no one has decided" about a document
    that has.
    """

    match = _MANAGED_AUTHORITY_RE.search(_read_text_or_empty(document_path))
    if match is None:
        return None
    return _AUTHORITY_FROM_MANIFEST.get(match.group("authority"))


def _read_text_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


#: The page id inside an `atls:managed` manifest line. A regex rather than the parser
#: because this runs over every `.md` in a directory, most of which are not managed
#: documents at all, and a parse failure on somebody's notes is not an error worth
#: raising -- it is simply not the file we are looking for.
_MANAGED_PAGE_RE = re.compile(r"<!--\s*atls:managed\s[^>]*?\bpage=(?P<page>[^\s>]+)")


def find_page_documents(directory: Path, page_id: str) -> tuple[Path, ...]:
    """Every document in one directory whose record names this page.

    Authority is a per-file marker, and one page can have both a Markdown and a
    storage copy sitting side by side. Marking only the file in hand leaves the
    other one still believing it may publish, which is the conflict the marker
    exists to prevent -- with an extra step.

    Scoped to a single directory, and that is a real limit rather than an
    oversight: nothing on this machine maps a page id to the copies of it, so a
    copy somewhere else is not covered and cannot be. What backstops that is not
    this marker but the push itself, which re-measures compatibility and
    candidate loss against fresh remote state every time.
    """

    found: list[Path] = []
    for path in sorted(directory.glob("*" + SUFFIX)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("page_id") == page_id:
            found.append(path.with_name(path.name[: -len(SUFFIX)]))

    # And the managed Markdown documents, found by their inline manifest.
    #
    # Searching only for sidecars made this blind exactly when it matters. AC1 stopped
    # the pull writing one, so a managed Markdown copy has no sidecar until something
    # asks for it -- and a storage pull beside that copy would report that it had moved
    # authority while leaving the Markdown file unmarked and still publishable. The
    # marker exists to prevent that conflict, so it cannot depend on the file it
    # replaced.
    seen = set(found)
    for path in sorted(directory.glob("*.md")):
        if path in seen or path.name.endswith(SUFFIX):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _MANAGED_PAGE_RE.search(text) is None:
            continue
        for match in _MANAGED_PAGE_RE.finditer(text):
            if match.group("page") == page_id:
                found.append(path)
                break

    return tuple(found)


def write_sidecar(document_path: Path, sidecar: Sidecar) -> Path:
    """Write the record beside its document, replacing any previous one.

    Written whole rather than patched: a sidecar holding a new version and an old
    base snapshot describes a page that never existed.
    """

    path = sidecar_path(document_path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(sidecar.to_json())
    temporary.replace(path)
    return path


__all__ = [
    "SCHEMA",
    "SUFFIX",
    "Sidecar",
    "SidecarUnusable",
    "find_page_documents",
    "read_authority",
    "read_sidecar",
    "read_xhtml_sidecar",
    "sidecar_path",
    "write_sidecar",
]
