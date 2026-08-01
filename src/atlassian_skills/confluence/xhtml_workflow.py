"""Editing a page as storage, for the documents Markdown cannot hold.

Some pages carry things Markdown has no place to put, and for those the honest
answer is not a lossy round trip -- it is to edit what the server actually
stores. The low-level ability was already here: `page get --body-repr=storage
--format=raw` reads it and `page update --body-format=storage --if-version`
writes it. What was missing is everything around that pair, which is why the
guidance to "use XHTML" left people composing commands from parts of an error
message.

Five commands, and the reason each exists:

    pull-xhtml      writes the storage document and records what it came from
    validate-xhtml  offline: is this still well-formed, and did an edit quietly
                    drop a macro's identity or an attachment reference?
    diff-xhtml      what the server holds now versus what is on disk
    push-xhtml      publishes exactly the document the caller approved
    set-authority   which representation may publish, so two of them cannot

The last one is the load-bearing part. If Markdown and storage can both publish
the same page, the conflict this design set out to remove comes back under a new
name -- so while a page is storage-managed, `push-md` refuses, by name.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import replace
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from atlassian_skills.confluence.identity_gate import MACRO_ELEMENT, TRACKED, find_dropped_attributes
from atlassian_skills.confluence.sidecar import (
    Sidecar,
    SidecarUnusable,
    find_page_documents,
    read_xhtml_sidecar,
    sidecar_path,
    write_sidecar,
)
from atlassian_skills.core.errors import StaleError, ValidationError

#: Declared so a fragment of Confluence storage parses on its own. The server
#: never sends these declarations -- the body is a fragment, not a document --
#: and without them every page fails to parse for a reason that has nothing to
#: do with the page.
NAMESPACES = {
    "ac": "http://atlassian.com/content",
    "ri": "http://atlassian.com/resource/identifier",
    "at": "http://atlassian.com/schema",
}

ATTACHMENT_ELEMENT = "ri:attachment"
ATTACHMENT_IDENTITY = ("ri:filename",)


def _sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _page_version(page: Any) -> int:
    version = getattr(page, "version", None)
    return int(getattr(version, "number", version) or 1)


def _wrapped(storage: str) -> str:
    declarations = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in NAMESPACES.items())
    return f"<root {declarations}>{storage}</root>"


def parse_storage(storage: str) -> ElementTree.Element:
    """Parse a storage fragment, or raise with something to act on.

    The parse errors a caller gets from a raw fragment name a missing namespace
    prefix, which sends them looking for a bug in the page rather than at the
    unclosed tag they just typed.
    """

    try:
        return ElementTree.fromstring(_wrapped(storage))
    except ElementTree.ParseError as error:
        raise ValidationError(
            "The storage document is not well-formed XML.",
            hint="Confluence storage is XML: every tag closes and every attribute is quoted.",
            context={"reason": "xhtml_not_well_formed", "detail": str(error)},
        ) from error


def _undeclared_prefixes(storage: str) -> tuple[str, ...]:
    """Namespace prefixes the document uses that this workflow cannot resolve.

    Reported rather than resolved. A prefix we do not know is a part of the page
    we cannot reason about, and silently declaring it would turn an unknown into
    something that merely parses.
    """

    used = set(re.findall(r"</?([A-Za-z][\w.-]*):", storage))
    used.update(re.findall(r"\s([A-Za-z][\w.-]*):[\w.-]+\s*=", storage))
    return tuple(sorted(used - set(NAMESPACES)))


def _transfer_authority(anchor: Path, page_id: str, *, to: str) -> list[dict[str, str]]:
    """Move every record for this page in one directory to the same authority.

    Returns what it changed, including the anchor, so a caller can see the scope
    it actually got. Silence here would read as "the whole page is covered",
    which is the claim this cannot make: a copy in another directory is not
    reachable from anything on this machine.
    """

    moved: list[dict[str, str]] = []
    for document in find_page_documents(anchor.parent, page_id):
        # `_authority_record`, not `_read_any`: a managed Markdown neighbour has no
        # sidecar until something asks for one, and refusing to mark it would leave it
        # believing it may publish -- the conflict this transfer exists to end.
        current = _authority_record(document, page_id=page_id)
        if current.authority == to:
            continue
        write_sidecar(document, replace(current, authority=to))
        moved.append({"path": str(document), "from": current.authority, "to": to})
    return moved


# --------------------------------------------------------------------------
# pull-xhtml
# --------------------------------------------------------------------------


def pull_xhtml(client: Any, page_id: str, *, output_path: Path) -> dict[str, Any]:
    """Write the storage document and the record of what it was pulled from.

    Byte-preserving on purpose: this file is what gets published, and a document
    that has been through a formatter is not the document that was read.
    """

    page = client.get_page(page_id)
    storage = getattr(page, "body_storage", None) or ""
    version = _page_version(page)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(storage, encoding="utf-8")

    site = getattr(client, "base_url", "") or ""
    sidecar = Sidecar(
        page_id=page_id,
        site=site,
        remote_version=version,
        remote_storage_sha256=_sha256(storage),
        converter="",
        profile="storage",
        base_markdown="",
        authority="xhtml",
        base_storage=storage,
    )
    write_sidecar(output_path, sidecar)

    # Every other copy of this page beside it, not just the file just written.
    # Marking only this one leaves a Markdown copy still believing it may
    # publish, which is the conflict the marker exists to prevent with an extra
    # step -- and the skill promised the opposite.
    transferred = _transfer_authority(output_path, page_id, to="xhtml")

    return {
        "status": "pulled",
        "page_id": page_id,
        "path": str(output_path),
        "sidecar": str(sidecar_path(output_path)),
        "authority_transferred": transferred,
        # Recorded on the way out so the caller does not have to hash the file to
        # find out what a later push will compare against.
        "remote_version": version,
        "remote_storage_sha256": sidecar.remote_storage_sha256,
        "authority": "xhtml",
        "next_actions": [
            {
                "label": "check the edited document before publishing it",
                "argv": ["confluence", "page", "xhtml", "validate", str(output_path), "--format=json"],
                "requires_user_approval": False,
            },
            {
                "label": "see what the server holds now against what is on disk",
                "argv": ["confluence", "page", "xhtml", "compare", page_id, str(output_path), "--format=json"],
                "requires_user_approval": False,
            },
        ],
    }


# --------------------------------------------------------------------------
# validate-xhtml
# --------------------------------------------------------------------------


def validate_xhtml(path: Path) -> dict[str, Any]:
    """Check an edited storage document without asking the server anything.

    Offline because the failures worth catching here are the ones an editor
    introduces -- an unclosed tag, a prefix nobody declared, a macro that lost
    its id to a careless selection -- and none of those need a round trip.

    The identity check needs the before-text, so it runs only when the record
    beside the file has one. Reporting "nothing was dropped" from a comparison
    that never happened is worse than reporting that it did not happen.
    """

    document = path.read_text(encoding="utf-8")
    parse_storage(document)

    findings: list[dict[str, Any]] = []
    undeclared = _undeclared_prefixes(document)
    if undeclared:
        findings.append({"code": "undeclared_namespace", "prefixes": list(undeclared)})

    identity_checked = False
    try:
        sidecar = read_xhtml_sidecar(path, page_id=_sidecar_page_id(path))
    except SidecarUnusable as unusable:
        findings.append({"code": "identity_check_skipped", "reason": unusable.reason})
    else:
        identity_checked = True
        for element, attributes in ((MACRO_ELEMENT, TRACKED), (ATTACHMENT_ELEMENT, ATTACHMENT_IDENTITY)):
            for loss in find_dropped_attributes(sidecar.base_storage, document, element=element, attributes=attributes):
                findings.append(
                    {
                        "code": "identity_dropped",
                        "element": element,
                        "attribute": loss.attribute,
                        "count": loss.detached,
                        "ambiguous": loss.ambiguous,
                    }
                )

    return {
        "status": "valid" if not findings else "findings",
        "path": str(path),
        "well_formed": True,
        "identity_checked": identity_checked,
        "findings": findings,
        "candidate_sha256": _sha256(document),
    }


def _sidecar_page_id(path: Path) -> str:
    """The page the record beside this file claims, so reading it can be refused.

    Read from the file rather than taken from the caller because `validate-xhtml`
    takes no page argument: the document and its record travel together, and
    asking the caller to restate the id invites them to state a different one.
    """

    import json

    try:
        payload = json.loads(sidecar_path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(payload.get("page_id", "")) if isinstance(payload, dict) else ""


# --------------------------------------------------------------------------
# diff-xhtml
# --------------------------------------------------------------------------


def diff_xhtml(client: Any, page_id: str, path: Path) -> dict[str, Any]:
    """What the server holds now, against what is on disk.

    Line-based, over the raw storage. A structural diff would read better and
    would also decide, on the caller's behalf, which differences are worth
    mentioning -- and the whole reason a page is on this path is that we could
    not classify its contents safely.
    """

    local = path.read_text(encoding="utf-8")
    page = client.get_page(page_id)
    remote = getattr(page, "body_storage", None) or ""

    diff = "".join(
        difflib.unified_diff(
            remote.splitlines(keepends=True),
            local.splitlines(keepends=True),
            fromfile="remote",
            tofile="local",
        )
    )
    identity = [
        {"element": element, "attribute": loss.attribute, "count": loss.detached, "ambiguous": loss.ambiguous}
        for element, attributes in ((MACRO_ELEMENT, TRACKED), (ATTACHMENT_ELEMENT, ATTACHMENT_IDENTITY))
        for loss in find_dropped_attributes(remote, local, element=element, attributes=attributes)
    ]

    return {
        "page_id": page_id,
        "identical": remote == local,
        "diff": diff,
        "remote_version": _page_version(page),
        "remote_storage_sha256": _sha256(remote),
        "candidate_sha256": _sha256(local),
        # The candidate hash is what `push-xhtml --accept-candidate` takes, so a
        # caller who has just read the diff has the token in hand.
        "identity_dropped": identity,
    }


# --------------------------------------------------------------------------
# push-xhtml
# --------------------------------------------------------------------------


def push_xhtml(
    client: Any,
    page_id: str,
    path: Path,
    *,
    if_version: int | None = None,
    accept_candidate: str | None = None,
    dry_run: bool = False,
    reason: str | None = None,
    minor_edit: bool = False,
) -> dict[str, Any]:
    """Publish exactly the document the caller approved, or refuse and say why.

    Three separate gates, and none of them subsumes another:

        the page has not moved since it was pulled   -- else someone's edit is lost
        `--if-version` matches, when given           -- the caller's own belief
        `--accept-candidate` matches this file       -- approval is for bytes,
                                                        not for an intention

    The third is the one that is easy to leave out and the one that matters most
    here. A storage document can be edited between the dry run that was reviewed
    and the push that publishes, and a hash is the only thing that notices.
    """

    document = path.read_text(encoding="utf-8")
    parse_storage(document)
    candidate_sha256 = _sha256(document)

    try:
        sidecar = read_xhtml_sidecar(path, page_id=page_id)
    except SidecarUnusable as unusable:
        raise ValidationError(
            "No record of what this storage document was pulled from.",
            hint="Pull the page with 'atls confluence page pull-xhtml' to write one.",
            context={"reason": unusable.reason, "detail": unusable.detail, "page_id": page_id},
        ) from unusable

    if sidecar.authority != "xhtml":
        raise ValidationError(
            "This page is managed as Markdown, so publishing storage would overwrite that.",
            hint="Switch with 'atls confluence page set-authority' if storage is meant to be authoritative.",
            context={"reason": "markdown_is_authoritative", "page_id": page_id},
        )

    page = client.get_page(page_id)
    remote = getattr(page, "body_storage", None) or ""
    remote_version = _page_version(page)
    remote_sha256 = _sha256(remote)

    if (remote_version, remote_sha256) != (sidecar.remote_version, sidecar.remote_storage_sha256):
        raise StaleError(
            "The page moved since this storage document was pulled.",
            hint="Pull again and reapply the edit; publishing now would overwrite a change nobody has seen.",
            context={
                "reason": "remote_stale",
                "expected_version": sidecar.remote_version,
                "server_version": remote_version,
                "next_actions": [
                    {
                        "label": "read what the server holds now against what is on disk",
                        "argv": ["confluence", "page", "xhtml", "compare", page_id, str(path), "--format=json"],
                        "requires_user_approval": False,
                    }
                ],
            },
        )

    if if_version is not None and if_version != remote_version:
        raise StaleError(
            "--if-version does not match the version on the server.",
            context={"reason": "if_version_mismatch", "expected_version": if_version, "server_version": remote_version},
        )

    identity = [
        {"element": element, "attribute": loss.attribute, "count": loss.detached, "ambiguous": loss.ambiguous}
        for element, attributes in ((MACRO_ELEMENT, TRACKED), (ATTACHMENT_ELEMENT, ATTACHMENT_IDENTITY))
        for loss in find_dropped_attributes(remote, document, element=element, attributes=attributes)
    ]

    if dry_run:
        return {
            "status": "dry_run",
            "page_id": page_id,
            "would_update": document != remote,
            "remote_version": remote_version,
            "candidate_sha256": candidate_sha256,
            "identity_dropped": identity,
            "next_actions": [
                {
                    "label": "publish this exact document",
                    "argv": [
                        "confluence",
                        "page",
                        "xhtml",
                        "push",
                        page_id,
                        "--xhtml-file",
                        str(path),
                        "--if-version",
                        str(remote_version),
                        "--accept-candidate",
                        candidate_sha256,
                    ],
                    "requires_user_approval": True,
                }
            ],
        }

    if accept_candidate != candidate_sha256:
        raise ValidationError(
            "Publishing storage requires the hash of the exact document being published.",
            hint="Run with --dry-run and pass back the candidate_sha256 it returns.",
            context={
                "reason": "candidate_not_approved",
                "candidate_sha256": candidate_sha256,
                **({"supplied": accept_candidate} if accept_candidate else {}),
            },
        )

    if document == remote:
        # Nothing to publish, and a version whose only change is that someone
        # pressed save is noise in a history other people read.
        return {"status": "unchanged", "page_id": page_id, "version": remote_version}

    version_options: dict[str, Any] = {}
    if reason is not None:
        version_options["reason"] = reason
    if minor_edit:
        version_options["minor_edit"] = True
    client.update_page(
        page_id=page_id,
        title=getattr(page, "title", ""),
        body=document,
        version_number=remote_version + 1,
        **version_options,
    )

    # Read back rather than trusting the response. The server normalises storage
    # on save, so what is on the page afterwards is the only account of what was
    # published -- and the sidecar has to record that, not what we sent.
    written = client.get_page(page_id)
    written_storage = getattr(written, "body_storage", None) or ""
    written_version = _page_version(written)

    # The file follows the page. Leaving the sent bytes on disk after the server
    # rewrote them means every later diff shows a difference nobody made, and an
    # author cannot tell their own pending edit from the server's housekeeping.
    # This file is the page, so after a publish it says what the page says.
    if written_storage != document:
        path.write_text(written_storage, encoding="utf-8")

    write_sidecar(
        path,
        Sidecar(
            page_id=page_id,
            site=sidecar.site,
            remote_version=written_version,
            remote_storage_sha256=_sha256(written_storage),
            converter=sidecar.converter,
            profile=sidecar.profile,
            base_markdown="",
            authority="xhtml",
            base_storage=written_storage,
        ),
    )

    return {
        "status": "updated",
        "page_id": page_id,
        "version": written_version,
        "remote_storage_sha256": _sha256(written_storage),
        # True when the server stored something other than what was sent, which
        # it routinely does. Said plainly rather than left for the next push to
        # discover as an unexplained difference -- and it means the local file
        # was rewritten to match, so an editor with the file open should reload.
        "server_normalized": written_storage != document,
    }


# --------------------------------------------------------------------------
# set-authority
# --------------------------------------------------------------------------


def set_authority(
    page_id: str,
    *,
    to: str,
    md_path: Path | None = None,
    xhtml_path: Path | None = None,
) -> dict[str, Any]:
    """Declare which representation may publish this page.

    Takes the files rather than only the page id, and that is not a shortcut:
    nothing on this machine maps an id to the documents pulled from it, and a
    command that claimed to switch authority while leaving a stale sidecar
    behind would be worse than no command at all -- the file it missed would go
    on believing it may publish.
    """

    if to not in {"markdown", "xhtml"}:
        raise ValidationError(
            "Authority is either 'markdown' or 'xhtml'.",
            context={"reason": "unknown_authority", "supplied": to},
        )
    if to == "markdown":
        _refuse_ungraded_markdown_authority(md_path=md_path, xhtml_path=xhtml_path, page_id=page_id)
    if md_path is None and xhtml_path is None:
        raise ValidationError(
            "Name the files whose authority is changing.",
            hint="Pass --md-file, --xhtml-file, or both. Nothing here maps a page id to local files.",
            context={"reason": "authority_target_required", "page_id": page_id},
        )

    changed: list[dict[str, str]] = []
    for path, kind in ((md_path, "markdown"), (xhtml_path, "xhtml")):
        if path is None:
            continue
        if not sidecar_path(path).exists():
            # AC1 stopped the pull writing a record unless it is asked for, so the old
            # refusal's advice -- "pull the page again to write one" -- became false: a
            # fresh pull writes nothing to refuse about. And the manifest cannot carry
            # this answer for a Markdown file, because managed Markdown admits only
            # `md` as its authority (§10.1) and a file that may not publish is exactly
            # the case being recorded.
            #
            # So an explicit `set-authority` creates the record. That is not the default
            # write §10.1 forbids; it is the user naming the file and asking for its
            # authority to change, which is as explicit as `--write-base-cache`.
            existing = _authority_record_from_document(path, page_id=page_id)
        else:
            existing = _read_any(path, page_id=page_id)

        write_sidecar(
            path,
            Sidecar(
                page_id=existing.page_id,
                site=existing.site,
                remote_version=existing.remote_version,
                remote_storage_sha256=existing.remote_storage_sha256,
                converter=existing.converter,
                profile=existing.profile,
                base_markdown=existing.base_markdown,
                authority=to,
                base_storage=existing.base_storage,
            ),
        )
        changed.append({"path": str(path), "kind": kind, "authority": to})
        # The other copies beside it, for the same reason `pull-xhtml` does it:
        # switching one file and leaving its sibling claiming authority is the
        # state this command exists to end.
        changed.extend(
            {"path": moved["path"], "kind": "sibling", "authority": to}
            for moved in _transfer_authority(path, page_id, to=to)
            if moved["path"] != str(path)
        )

    return {
        "status": "authority_set",
        "page_id": page_id,
        "authority": to,
        "files": changed,
        # Said in the payload, not only in the docs. The marker covers the files
        # named and their neighbours; a copy in another directory is not reachable
        # and is not covered.
        "scope": "files_named_and_their_directory",
    }


def _refuse_ungraded_markdown_authority(*, md_path: Path | None, xhtml_path: Path | None, page_id: str) -> None:
    """§10.2: authority must not become a way to publish a body no grade approved.

    Measured before this existed: `set-authority --to markdown` on a page whose losses
    cannot be classified made its Markdown copy publishable again. The grade said the page
    must not be managed as Markdown and the marker overrode it, which is the one thing
    §10.2 names.

    §10.2 permits either a re-grade or disabling `--to markdown`. This is the re-grade, and
    it needs no client: what gets graded is the storage, and the storage is on disk
    whenever the caller has an exact copy of the page. So handing authority *to* Markdown
    requires naming that copy.

    That is a real requirement rather than a formality. Going to Markdown is the direction
    that grants permission, and a permission granted from a document nobody re-read is a
    permission granted on the strength of whatever was true when it was last pulled. Going
    to `xhtml` needs nothing: it only ever removes permission.
    """

    # The guard has to cover what the *sweep* will touch, not what the caller named.
    #
    # Review R3 found the hole: with only `--xhtml-file`, this returned early on the
    # reasoning that there was nothing to grant — and then `_transfer_authority` swept the
    # named file's directory and wrote Markdown authority onto the managed Markdown sibling
    # sitting beside it. `build_managed_preflight` then reads that sidecar as `markdown`, so
    # the `xhtml_is_authoritative` refusal stops applying and an `xhtml_required` document
    # reaches the Markdown publish preflight through exactly the authority-only route §10.2
    # closes.
    #
    # So the subject is every document the transfer will mark, and `md_path` is only one of
    # them.
    targets = _markdown_documents_the_transfer_would_mark(md_path=md_path, xhtml_path=xhtml_path, page_id=page_id)
    if not targets:
        return
    subject = targets[0]
    storage = _local_storage_for(xhtml_path)
    if storage is None:
        raise ValidationError(
            "Handing authority to Markdown needs the storage copy of the page, so its losses can be graded.",
            hint=(
                "Pass --xhtml-file with an exact copy (page xhtml pull ID --output page.xhtml). "
                "Without it the grade cannot be re-checked, and granting Markdown authority on trust "
                "is what §10.2 forbids."
            ),
            context={"reason": "authority_grade_unavailable", "page_id": page_id, "path": str(subject)},
        )
    from atlassian_skills.confluence.compatibility import compatibility_payload

    payload = compatibility_payload(page_id, storage, document_path=str(subject))
    if not payload.get("canonical_write_permitted"):
        raise ValidationError(
            "This page's losses do not allow it to be managed as Markdown, so authority cannot move there.",
            hint="See compatibility.next_actions for the workflow this page does support.",
            context={
                "reason": "authority_refused_by_grade",
                "page_id": page_id,
                "grade": payload.get("status"),
                "path": str(subject),
            },
        )


def _markdown_documents_the_transfer_would_mark(
    *, md_path: Path | None, xhtml_path: Path | None, page_id: str
) -> list[Path]:
    """Every managed Markdown document `set_authority` would write `markdown` onto.

    The named `--md-file`, plus the siblings the directory sweep reaches. Storage documents are
    excluded: marking one `markdown` removes its permission rather than granting any, which is
    the direction that needs no evidence.
    """

    from atlassian_skills.confluence.sidecar import find_page_documents

    found: list[Path] = []
    for anchor in (md_path, xhtml_path):
        if anchor is None:
            continue
        for document in [anchor, *find_page_documents(anchor.parent, page_id)]:
            if document.suffix.lower() in {".xhtml", ".xml"} or document in found:
                continue
            found.append(document)
    return found


def _local_storage_for(xhtml_path: Path | None) -> str | None:
    """The page's storage, from the exact copy or the record beside it.

    The document itself is preferred: `page xhtml pull` is byte-preserving, so the file
    *is* the storage. The sidecar's `base_storage` is the fallback for a copy that has been
    edited since -- grading the pulled base is still grading this page, and grading a
    half-finished local edit would not be.
    """

    if xhtml_path is None:
        return None
    try:
        record = read_xhtml_sidecar(xhtml_path, page_id=_sidecar_page_id(xhtml_path))
    except Exception:
        record = None
    if record is not None and record.base_storage:
        # The record carries the storage *and* its hash. Reading one and trusting it while the thing
        # that would catch a mismatch sits in the same file is not a check.
        #
        # Review R3 (R3-4) reproduced what that allowed: a sidecar copied from another page, left
        # stale, or edited by hand offers a Markdown-safe `base_storage` while its own
        # `remote_storage_sha256` describes an `xhtml_required` page. The grade is computed from the
        # injected body, approves, and the transfer writes `markdown` authority onto the managed
        # Markdown sibling.
        #
        # A mismatch means the record cannot say what this page holds, so it supplies no baseline at
        # all -- the caller then gets `authority_grade_unavailable`, which is the fail-closed answer
        # and names what to do about it.
        recorded = record.remote_storage_sha256 or ""
        actual = "sha256:" + hashlib.sha256(record.base_storage.encode("utf-8")).hexdigest()
        if recorded and recorded != actual:
            return None
        return record.base_storage
    try:
        return xhtml_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _authority_record(path: Path, *, page_id: str) -> Sidecar:
    """The authority record for this document, from the sidecar or from the document.

    One place makes this decision, so `set-authority` and the sibling transfer cannot
    disagree about whether a file without a sidecar can be marked.
    """

    if sidecar_path(path).exists():
        return _read_any(path, page_id=page_id)
    return _authority_record_from_document(path, page_id=page_id)


def _authority_record_from_document(path: Path, *, page_id: str) -> Sidecar:
    """Build the authority record from what the document already says about itself.

    Everything needed is in the v3 manifest -- page, site, version, storage hash,
    converter, profile. `base_markdown` is left empty on purpose: changing authority
    does not read a before-text (see `_read_any`), and inventing one here would put a
    baseline nobody verified where the merge path looks for one.
    """

    from atlassian_skills.core.managed_file import read_managed_utf8
    from atlassian_skills.core.managed_manifest import ManagedManifestError, strip_managed_manifest

    try:
        _body, manifest = strip_managed_manifest(read_managed_utf8(path))
    except (OSError, ValidationError, ManagedManifestError) as error:
        raise ValidationError(
            f"{path} is not a managed document, so its authority cannot be changed.",
            hint="Name a file pulled by `page md pull` or `page xhtml pull`.",
            context={"reason": "not_a_managed_document", "path": str(path)},
        ) from error
    if manifest.page != page_id:
        raise ValidationError(
            f"{path} is bound to a different page.",
            context={"reason": "sidecar_page_mismatch", "path": str(path), "page_id": page_id},
        )
    return Sidecar(
        page_id=manifest.page,
        site=manifest.site,
        remote_version=manifest.remote_version,
        remote_storage_sha256=manifest.remote_storage,
        converter=manifest.converter,
        profile=manifest.profile,
        base_markdown="",
    )


def _read_any(path: Path, *, page_id: str) -> Sidecar:
    """Load a record without caring which before-text it carries.

    Changing authority does not read either one, and refusing here because the
    other kind of snapshot is absent would make the command unusable on exactly
    the files it is for.
    """

    import json

    try:
        payload = json.loads(sidecar_path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(
            f"The record beside {path} could not be read.",
            context={"reason": "sidecar_unreadable", "path": str(path)},
        ) from error
    if not isinstance(payload, dict) or payload.get("page_id") != page_id:
        raise ValidationError(
            f"The record beside {path} belongs to a different page.",
            hint="Managed files get copied between directories; check the path.",
            context={
                "reason": "sidecar_page_mismatch",
                "path": str(path),
                "page_id": page_id,
            },
        )
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


__all__ = [
    "NAMESPACES",
    "diff_xhtml",
    "parse_storage",
    "pull_xhtml",
    "push_xhtml",
    "set_authority",
    "validate_xhtml",
]
