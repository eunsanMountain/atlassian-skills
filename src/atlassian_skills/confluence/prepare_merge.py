"""Lay out the three versions an agent needs to resolve a stale page itself.

A stale push is correct to refuse and, on its own, a dead end: pull again and redo
the edit by hand, which is what sends people back to the browser. Measured across
55 live pages, every managed push against a page someone else had touched ended
there.

The refusal already says *whether* the two edits combine. This says *what they
are*, as three files on disk, so the party that can read a document for meaning
does the merging.

The split is deliberate and it is the whole design:

    atls    holds the base, fetches the remote, writes the three files, states
            the version and hash the result must be published against, and
            verifies whatever comes back
    agent   reads base-to-local and base-to-remote, merges with the document's
            meaning in mind, and asks the author when the two disagree about
            what the page should say

Neither publishes a stale page on its own. The line-based merge offered here is a
suggestion, not a verdict: it is conservative by construction, so it refuses
things a person would merge in a moment, and it aligns repeated lines -- table
rows, list items -- by position, where position means nothing.

Nothing here writes to Confluence. It reads the page and writes local files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cfxmark

from atlassian_skills.confluence.base_resolver import BaseResolution, project, resolve_base
from atlassian_skills.confluence.merge import merge3
from atlassian_skills.confluence.sidecar import (
    Sidecar,
    sidecar_path,
    write_sidecar,
)
from atlassian_skills.core.errors import ValidationError
from atlassian_skills.core.managed_file import read_managed_utf8
from atlassian_skills.core.managed_manifest import (
    CURRENT_MANAGED_MANIFEST_VERSION,
    ManagedManifest,
    ManagedManifestError,
    canonical_asset_set_sha256,
    canonical_content_sha256,
    extract_asset_records,
    parse_managed_document,
    parse_managed_manifest,
    serialize_managed_manifest,
)
from atlassian_skills.core.site_identity import site_fingerprint

SCHEMA = "atls-merge-workspace-v1"

#: What the three files were built from, written beside them. Read back by
#: `finalize-merge`, which is the only reason it exists.
BASIS = "workspace.json"
BASIS_SCHEMA = "atls-merge-basis-v1"


@dataclass(frozen=True)
class MergeWorkspace:
    """Three versions of a document, and what a publish of the result must match."""

    page_id: str
    managed_path: Path
    #: `None` when §5.4 exhausted every source. The workspace is still written --
    #: two-way, with the reason -- because a caller who can read the document for
    #: meaning can still reconcile it, and refusing outright is what sent people back
    #: to the browser.
    base_path: Path | None
    local_path: Path
    remote_path: Path
    candidate_path: Path | None
    conflicts: tuple[dict[str, Any], ...]
    remote_version: int
    remote_storage_sha256: str
    base_resolution: BaseResolution | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "page_id": self.page_id,
            "base": str(self.base_path) if self.base_path else None,
            # Stated, never implied. A caller that cannot tell "no base" from "base
            # that happened to be empty" will merge the wrong thing exactly once.
            "base_available": self.base_path is not None,
            "base_source": self.base_resolution.source if self.base_resolution else None,
            "base_unavailable_reason": (
                self.base_resolution.reason if self.base_resolution and self.base_path is None else None
            ),
            "base_attempts": [dict(item) for item in (self.base_resolution.attempts if self.base_resolution else ())],
            "local": str(self.local_path),
            "remote": str(self.remote_path),
            "candidate": str(self.candidate_path) if self.candidate_path else None,
            # Empty when the suggestion is clean. Present with locations when it
            # is not, so an agent can go straight to the paragraphs in question
            # rather than diffing the three files itself to find them.
            "conflicts": list(self.conflicts),
            "remote_version": self.remote_version,
            "remote_storage_sha256": self.remote_storage_sha256,
            # These files are plain Markdown, because an agent has to read and
            # edit them. `push-md` takes only a managed document, so the next
            # step is to rebind -- not to publish. Pointing straight at push-md
            # here produced a workflow that ended one step short of the thing it
            # exists for: the merged text was right there and nothing could
            # publish it.
            "next_actions": [
                {
                    "label": "rebind the merged body to the current remote, as a publishable document",
                    "argv": [
                        "confluence",
                        "page",
                        "md",
                        "finalize-merge",
                        self.page_id,
                        "--md-file",
                        str(self.managed_path),
                        "--candidate",
                        str(self.candidate_path or self.local_path),
                        "--format=json",
                    ],
                    "requires_user_approval": False,
                }
            ],
        }


def _sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def prepare_merge(
    client: Any,
    page_id: str,
    managed_path: Path,
    *,
    output_dir: Path,
    base_file: Path | None = None,
) -> MergeWorkspace:
    """Write base, local and remote side by side, and suggest a merge.

    Refuses rather than guessing when the base snapshot is unusable -- missing,
    corrupt, or belonging to another page. Merging against the wrong base
    produces a document that reads perfectly well and is wrong, which is worse
    than having no merge at all.
    """

    managed = read_managed_utf8(managed_path)
    # The content hash in the manifest is of what the pull wrote, and this path
    # exists precisely for files an author has since edited. Verifying it here
    # would refuse every input this command is for.
    document = parse_managed_document(
        managed, assets=extract_asset_records(managed), verify_content=False, verify_assets=False
    )
    page = client.get_page(page_id)
    remote_storage = getattr(page, "body_storage", None) or ""
    remote_markdown = project(remote_storage, document.manifest)

    # §5.4, in order: the server's own history first, then a sidecar if one happens
    # to be lying beside the document, then a base the caller names. The sidecar is
    # no longer the only source and no longer required -- which is what lets AC1 stop
    # writing one on every pull.
    #
    # An integrity failure raises out of here: a version whose bytes have changed, a
    # converter we are not running, a projection that does not reproduce the recorded
    # hash. Those mean the recorded binding is wrong, and no other source is more
    # trustworthy than the one that just disagreed.
    resolution = resolve_base(
        client,
        page_id,
        document.manifest,
        cache_path=managed_path,
        base_file=base_file,
    )

    # The header banner is written by the converter, not by an author, and only
    # some of the three carry it. Left in, it reads as an edit the remote made to
    # every document and turns every merge into a conflict about a sentence
    # nobody typed.
    strip = cfxmark.strip_header_notice
    local, remote = strip(document.content), strip(remote_markdown)
    base = strip(resolution.markdown) if resolution.markdown is not None else None

    output_dir.mkdir(parents=True, exist_ok=True)
    local_path = output_dir / "local.md"
    remote_path = output_dir / "remote.md"
    written: list[tuple[Path, str]] = [(local_path, local), (remote_path, remote)]
    base_path: Path | None = None
    if base is not None:
        base_path = output_dir / "base.md"
        written.append((base_path, base))
    for path, content in written:
        path.write_text(content, encoding="utf-8")

    # §5.4 step 4: with no base, the caller gets `L` and `R` and an explicit
    # statement that there is none. What must not happen is a merge that guesses --
    # applying either side wholesale overwrites the other, silently, in the direction
    # the merge existed to prevent.
    candidate_path: Path | None = None
    result = merge3(base, local, remote) if base is not None else None
    if result is not None and result.clean:
        candidate_path = output_dir / "candidate.md"
        candidate_path.write_text(result.require_clean(), encoding="utf-8")

    # Empty with no base, and that is not the same as "no conflicts". A conflict is a
    # statement about where two edits disagree RELATIVE TO a common ancestor; without
    # one there is nothing to be relative to, and the whole document is the
    # disagreement. `base_available: false` in the payload is what says so — reading
    # an empty conflict list as "they merge cleanly" is the mistake this comment
    # exists to prevent.
    conflicts = (
        tuple(
            {
                "base_start": conflict.base_start,
                "base_end": conflict.base_end,
                "local": list(conflict.local),
                "remote": list(conflict.remote),
            }
            for conflict in result.conflicts
        )
        if result is not None
        else ()
    )

    version = int(getattr(getattr(page, "version", None), "number", None) or 1)
    # Recorded on disk, not just returned. `finalize-merge` runs as a separate
    # command -- possibly after the agent has asked a person something -- and it
    # has to be able to prove the page has not moved since these three files
    # were written. Without that proof it can only re-read the remote and hope.
    (output_dir / BASIS).write_text(
        json.dumps(
            {
                "schema": BASIS_SCHEMA,
                "page_id": page_id,
                "remote_version": version,
                "remote_storage_sha256": _sha256(remote_storage),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return MergeWorkspace(
        page_id=page_id,
        managed_path=managed_path,
        base_path=base_path,
        base_resolution=resolution,
        local_path=local_path,
        remote_path=remote_path,
        candidate_path=candidate_path,
        conflicts=conflicts,
        remote_version=int(version),
        remote_storage_sha256=_sha256(remote_storage),
    )


def write_workspace_report(workspace: MergeWorkspace) -> str:
    return json.dumps(workspace.to_dict(), ensure_ascii=False, indent=2)


def finalize_merge(
    client: Any,
    page_id: str,
    managed_path: Path,
    candidate_path: Path,
    *,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Rebind a merged body to the current remote, as a file that can be published.

    The half that was missing. `prepare_merge` writes plain Markdown -- correctly,
    because an agent has to read and edit it -- and `push-md` accepts only a
    managed document. So the workflow ended one step short of the thing it exists
    for: the merged text was right there and nothing could publish it.

    Rewriting the manifest is the one part of a merge that must not be done by
    hand. It binds page, site, remote version, source hash, body hash and asset
    set together, and a document whose manifest disagrees with its body is either
    refused or -- worse -- published against a version nobody checked.

    So the split stays exactly where it was. The agent decides what the page
    should say; this decides nothing and only re-binds.

    Writes a new file rather than the canonical one. Until the push succeeds,
    nothing has been published, and a canonical file already claiming the new
    remote version would be asserting something untrue about the server.
    """

    managed = read_managed_utf8(managed_path)
    original = parse_managed_document(
        managed, assets=extract_asset_records(managed), verify_content=False, verify_assets=False
    )
    if original.manifest.page != page_id:
        raise ValidationError(
            "The managed file belongs to a different page.",
            context={"reason": "managed_authority_mismatch", "page_id": page_id},
        )

    body = _candidate_body(candidate_path)

    page = client.get_page(page_id)
    remote_storage = getattr(page, "body_storage", None) or ""
    remote_version = int(getattr(getattr(page, "version", None), "number", None) or 1)
    remote_sha256 = f"sha256:{_sha256(remote_storage)}"

    # The page must be exactly what `prepare-merge` laid out. If it moved again
    # while the agent was reading the two diffs, the merge in hand was made
    # against a version that no longer exists, and rebinding it to the current
    # one publishes over a change nobody has read -- silently, because every
    # later check would then pass. Measured: it destroyed the third party's edit.
    #
    # Not re-merged automatically. The whole design rests on an agent having read
    # both diffs, and it has not read this one.
    _assert_prepared_against(candidate_path, page_id, remote_version, _sha256(remote_storage), str(managed_path))

    site = getattr(client, "base_url", None)
    if not isinstance(site, str):
        raise ValidationError("Rebinding a merged document requires the configured Confluence base URL")

    assets = extract_asset_records(body)
    # `R`: the current remote, projected with the document's own options. §5.1 defines
    # `base_md` as the hash of a REMOTE projection -- `M0`, `R` or `R2` -- and never of
    # the local body. Computed here rather than beside the sidecar below, because the
    # manifest binds to it and the sidecar merely caches it.
    remote_markdown = project(remote_storage, original.manifest)
    manifest = ManagedManifest(
        v=CURRENT_MANAGED_MANIFEST_VERSION,
        page=page_id,
        site=site_fingerprint(site),
        # The version the merge was actually made against, re-read here rather
        # than carried from the workspace. Between preparing a merge and
        # finishing one an agent reads two diffs and possibly asks a person, and
        # a page can move again in that time.
        remote_version=remote_version,
        remote_storage=remote_sha256,
        # hash(R), not hash(E). The merged body `E` is what the agent produced and no
        # remote version has ever held it, so recording it makes the next comparison
        # measure the local edit against itself -- a real remote change then shows up
        # as no change at all. BASELINE.md records this shape as one of the two defects
        # that shaped the plan; this is the finalize path it names.
        base_md=canonical_content_sha256(cfxmark.strip_header_notice(remote_markdown)),
        assets=canonical_asset_set_sha256(assets),
        converter=f"cfxmark/{cfxmark.__version__}",
        profile="markdown-first",
        passthrough=original.manifest.passthrough,
    )
    merged = serialize_managed_manifest(manifest) + "\n" + body

    # Beside the original by default, not beside the candidate. Managed asset
    # references resolve relative to the Markdown file that holds them, so a
    # merged document in `page.md.merge/` would look for `assets/x.png` inside
    # the workspace directory and not find the author's file.
    destination = output_path or managed_path.with_name(f"{managed_path.stem}.merged{managed_path.suffix}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(merged, encoding="utf-8")

    # The base for any *later* merge of this file is the remote as it stands now,
    # not the merged body: that is the same state a pull-then-edit produces, and
    # it is what makes a second round of this workflow behave like the first.
    write_sidecar(
        destination,
        Sidecar(
            page_id=page_id,
            site=site_fingerprint(site),
            remote_version=remote_version,
            remote_storage_sha256=remote_sha256,
            converter=f"cfxmark {cfxmark.__version__}",
            profile="editable",
            base_markdown=remote_markdown,
        ),
    )

    return {
        "schema": "atls-merged-document-v1",
        "status": "rebound",
        "page_id": page_id,
        "path": str(destination),
        "sidecar": str(sidecar_path(destination)),
        "remote_version": remote_version,
        "remote_storage_sha256": remote_sha256,
        "next_actions": [
            {
                "label": "check the merged document against the page it will be published to",
                "argv": [
                    "confluence",
                    "page",
                    "md",
                    "push",
                    page_id,
                    "--md-file",
                    str(destination),
                    "--dry-run",
                    "--format=json",
                ],
                "requires_user_approval": False,
            },
            {
                "label": "publish the merged document",
                "argv": [
                    "confluence",
                    "page",
                    "md",
                    "push",
                    page_id,
                    "--md-file",
                    str(destination),
                    "--if-version",
                    str(remote_version),
                ],
                "requires_user_approval": True,
            },
        ],
    }


def _candidate_body(candidate_path: Path) -> str:
    """The merged body, with a leading manifest line removed and nothing else.

    Dropping every line containing `atls:managed` deleted ordinary prose: a
    document explaining this format, or a code sample printing the string, is
    content. Only the exact first line counts, and it only counts when it parses
    as a manifest.

    A manifest anywhere else is not stripped either -- it is refused. A candidate
    with a manifest in the middle is not a document we understand, and guessing
    which half the author meant is how a merge quietly loses a section.
    """

    text = candidate_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    body_lines = lines
    if lines and _is_manifest_line(lines[0]):
        body_lines = lines[1:]
    for number, line in enumerate(body_lines, start=2 if body_lines is not lines else 1):
        if _is_manifest_line(line):
            raise ValidationError(
                "The merged document carries a managed manifest below its first line.",
                hint="Keep the merged body as plain Markdown; finalize-merge writes the manifest.",
                context={
                    "reason": "candidate_manifest_misplaced",
                    "line": number,
                    "path": str(candidate_path),
                },
            )
    return "\n".join(body_lines).lstrip("\n")


def _is_manifest_line(line: str) -> bool:
    """Whether this line is a managed manifest, asked of the parser not of a substring.

    `"atls:managed" in line` was the test, and it matched a sentence about the
    format and a `print("atls:managed")` in a code block.
    """

    try:
        parse_managed_manifest(line)
    except ManagedManifestError:
        return False
    return True


def _assert_prepared_against(
    candidate_path: Path,
    page_id: str,
    remote_version: int,
    remote_storage_sha256: str,
    managed_path: str,
) -> None:
    """Refuse unless the page is still what `prepare-merge` read.

    The basis is written beside the three files. If it is missing -- an agent
    assembled a candidate by hand, or moved it -- there is nothing to check
    against, and this refuses rather than proceeding on an unverifiable merge.
    """

    basis_path = candidate_path.parent / BASIS
    reason = None
    recorded: dict[str, Any] = {}
    try:
        recorded = json.loads(basis_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        reason = "merge_basis_missing"
    else:
        if not isinstance(recorded, dict) or recorded.get("schema") != BASIS_SCHEMA:
            reason = "merge_basis_unreadable"
        elif recorded.get("page_id") != page_id:
            reason = "merge_basis_page_mismatch"
        elif (recorded.get("remote_version"), recorded.get("remote_storage_sha256")) != (
            remote_version,
            remote_storage_sha256,
        ):
            reason = "remote_changed_since_prepare"

    if reason is None:
        return
    raise ValidationError(
        "The page is not the one this merge was prepared against.",
        hint=(
            "Run prepare-merge again and read the two diffs afresh. The merge in hand was made "
            "against a version that no longer exists, and publishing it would overwrite a change "
            "nobody has read."
        ),
        context={
            "reason": reason,
            "page_id": page_id,
            "prepared_version": recorded.get("remote_version"),
            "server_version": remote_version,
            "next_actions": [
                {
                    "label": "lay the merge out again against the page as it stands now",
                    "argv": [
                        "confluence",
                        "page",
                        "md",
                        "prepare-merge",
                        page_id,
                        "--md-file",
                        managed_path,
                        "--format=json",
                    ],
                    "requires_user_approval": False,
                }
            ],
        },
    )


__all__ = [
    "SCHEMA",
    "MergeWorkspace",
    "finalize_merge",
    "prepare_merge",
    "write_workspace_report",
]
