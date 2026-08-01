"""Compare, reconcile, record, rebaseline — the four steps of a stale document.

The CLI's job in a reconciliation is mechanical and the agent's job is not. This
module holds the mechanical half: fetch the current remote, resolve the base, say
what differs, and — when the agent hands back a merged body — rebind the local file
to the remote it was merged against. It never publishes. §7.2 through §7.5.

## The fingerprint is the whole safety story

Between `compare` and `record` an agent reads two diffs and may ask a person. That
takes as long as it takes, and in that time the page can move again. So `compare`
returns a fingerprint of everything the reconciliation was made against, and `record`
refuses unless a fresh read still produces the same one:

    site, page, remote version, remote storage hash, the remote projection,
    the base and where it came from, and the canonical body as it was

Both sides are in there deliberately. A remote that moved and a local file that was
edited are different accidents with the same consequence — a record that writes a
body reconciled against something that is no longer there — so both are named
(`remote_changed_since_compare`, `local_changed_since_compare`) rather than collapsed
into one "stale".

The fingerprint is not stored anywhere and is not replayable. §7.4 gives it the same
treatment the migration consent fingerprint already has: it lives in the conversation
until it is used, and a change to any input invalidates it.

## record is the only step that may replace a canonical body

`compare` and `prepare-reconcile` write zero canonical bytes. `record` writes exactly
one file, atomically, and reports the body hash before and after. `rebaseline` moves
the baseline and leaves the body alone. None of the four sends anything to Confluence.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import cfxmark

from atlassian_skills.confluence.base_resolver import BaseResolution, project, resolve_base
from atlassian_skills.core.errors import ValidationError
from atlassian_skills.core.managed_file import read_managed_utf8
from atlassian_skills.core.managed_manifest import (
    CURRENT_MANAGED_MANIFEST_VERSION,
    ManagedManifest,
    canonical_content_sha256,
    extract_asset_records,
    parse_managed_document,
    serialize_managed_manifest,
)

COMPARE_SCHEMA = "atls-compare-v1"
FINGERPRINT_SCHEMA = "atls-compare-fingerprint-v1"
WORKSPACE_SCHEMA = "atls-reconcile-workspace-v1"
RECORD_SCHEMA = "atls-reconciled-record-v1"
REBASELINE_SCHEMA = "atls-rebaseline-v1"


def _sha256(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class Comparison:
    """What `compare` found, and what a record of it must still match."""

    page_id: str
    manifest: ManagedManifest
    local: str
    remote: str
    remote_version: int
    remote_storage_sha256: str
    resolution: BaseResolution
    #: The canonical file exactly as it was read, so a record can tell an edited file
    #: from the one that was compared.
    canonical_text: str

    @property
    def base(self) -> str | None:
        return self.resolution.markdown

    @property
    def stale(self) -> bool:
        return self.remote_storage_sha256 != self.manifest.remote_storage

    def remote_fingerprint(self) -> str:
        """Everything the reconciliation depended on that lives on the server."""

        return _sha256(
            json.dumps(
                {
                    "schema": FINGERPRINT_SCHEMA,
                    "site": self.manifest.site,
                    "page": self.page_id,
                    "remote_version": self.remote_version,
                    "remote_storage": self.remote_storage_sha256,
                    "remote_md": canonical_content_sha256(self.remote),
                    "base_md": canonical_content_sha256(self.base) if self.base is not None else None,
                    "base_source": self.resolution.source,
                    "converter": f"cfxmark/{cfxmark.__version__}",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    def local_fingerprint(self) -> str:
        """Everything it depended on that lives in the working file."""

        return _sha256(
            json.dumps(
                {
                    "schema": FINGERPRINT_SCHEMA,
                    "local_body": canonical_content_sha256(self.local),
                    "canonical_file": _sha256(self.canonical_text),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    def fingerprint(self) -> str:
        """The two halves, joined.

        Compound rather than a single digest so `record` can say WHICH side moved.
        §7.4 names `local_changed_since_compare` and `remote_changed_since_compare`
        separately because they call for different next steps -- re-read and re-merge,
        or re-compare against the file as it now is -- and a single opaque token can
        only report that something did.

        Still opaque: both halves are hashes over inputs a caller cannot reconstruct,
        so this cannot be forged by editing a field. Still not replayable: it is
        derived on demand, stored nowhere, and every input change invalidates it.
        """

        return f"remote:{self.remote_fingerprint()}.local:{self.local_fingerprint()}"

    def diff(self, *, left: str = "base", right: str = "local") -> str:
        sides = {"base": self.base or "", "local": self.local, "remote": self.remote}
        return "".join(
            difflib.unified_diff(
                sides[left].splitlines(keepends=True),
                sides[right].splitlines(keepends=True),
                fromfile=left,
                tofile=right,
            )
        )


def compare(
    client: Any,
    page_id: str,
    managed_path: Path,
    *,
    base_file: Path | None = None,
) -> Comparison:
    """Read the page, resolve the base, and change nothing. §7.2.

    PUT 0 and canonical write 0. The one canonical comparison command, and three-way:
    an edit somebody else made since the pull shows up here rather than being
    discovered by a refused push. `--view=diff` renders the same comparison as text.

    `page diff-local` is a different question -- local against the recorded base, so it
    cannot see the remote at all -- and is kept only as a hidden compatibility spelling
    for callers that already type it.
    """

    canonical_text = read_managed_utf8(managed_path)
    document = parse_managed_document(
        canonical_text,
        assets=extract_asset_records(canonical_text),
        # This command exists for files an author has edited, so the recorded content
        # hash is expected not to match. That is `dirty`, which is a state, not
        # `invalid`, which is a refusal -- §P2's fourth task, and the distinction the
        # payload reports rather than conflates.
        verify_content=False,
        verify_assets=False,
    )
    page = client.get_page(page_id)
    if str(getattr(page, "id", page_id)) != page_id:
        raise ValidationError(
            "The page returned is not the page requested.",
            context={"reason": "page_identity_mismatch", "page_id": page_id},
        )
    remote_storage = getattr(page, "body_storage", None) or ""
    remote_version = int(getattr(getattr(page, "version", None), "number", None) or 1)

    resolution = resolve_base(client, page_id, document.manifest, cache_path=managed_path, base_file=base_file)
    return Comparison(
        page_id=page_id,
        manifest=document.manifest,
        local=cfxmark.strip_header_notice(document.content),
        remote=cfxmark.strip_header_notice(project(remote_storage, document.manifest)),
        remote_version=remote_version,
        remote_storage_sha256=_sha256(remote_storage),
        resolution=resolution,
        canonical_text=canonical_text,
    )


def compare_payload(comparison: Comparison, *, view: str = "summary") -> dict[str, Any]:
    """The JSON a caller branches on, with concrete argv and no placeholders."""

    local_dirty = canonical_content_sha256(comparison.local) != comparison.manifest.base_md
    payload: dict[str, Any] = {
        "schema": COMPARE_SCHEMA,
        "page_id": comparison.page_id,
        "manifest_version": comparison.manifest.v,
        "stale": comparison.stale,
        # Two independent facts, and reporting one for the other is how a caller ends
        # up re-pulling over an edit. `dirty` says the author changed the file;
        # `stale` says somebody changed the page.
        "local_dirty": local_dirty,
        "base_available": comparison.base is not None,
        "base_source": comparison.resolution.source,
        "base_unavailable_reason": comparison.resolution.reason if comparison.base is None else None,
        "base_attempts": [dict(item) for item in comparison.resolution.attempts],
        "remote_version": comparison.remote_version,
        "remote_storage_sha256": comparison.remote_storage_sha256,
        "compare_fingerprint": comparison.fingerprint(),
        "differs": {
            "local_vs_remote": comparison.local != comparison.remote,
            "base_vs_local": (comparison.base != comparison.local) if comparison.base is not None else None,
            "base_vs_remote": (comparison.base != comparison.remote) if comparison.base is not None else None,
        },
    }
    if view == "diff":
        payload["diff"] = {
            "base_vs_local": comparison.diff(left="base", right="local") if comparison.base else None,
            "base_vs_remote": comparison.diff(left="base", right="remote") if comparison.base else None,
            "local_vs_remote": comparison.diff(left="local", right="remote"),
        }
    payload["next_actions"] = _compare_next_actions(comparison)
    return payload


def _compare_next_actions(comparison: Comparison) -> list[dict[str, Any]]:
    managed = str(comparison.manifest.page)
    actions: list[dict[str, Any]] = []
    if comparison.stale:
        actions.append(
            {
                "label": "lay out base, local and remote so the merge can be read",
                "argv": [
                    "confluence",
                    "page",
                    "md",
                    "prepare-reconcile",
                    managed,
                    "--md-file",
                    "<the file you just compared>",
                    "--format=json",
                ],
                "requires_user_approval": False,
            }
        )
    return actions


def write_workspace(
    comparison: Comparison,
    *,
    output_dir: Path,
    managed_path: Path,
) -> dict[str, Any]:
    """§7.3. Lay the three versions out in a run-owned directory and report.

    Writes `base.md`, `local.md`, `remote.md` and `report.json`, and no canonical
    bytes. A `suggested.md` is deliberately absent: §7.3 permits one and calls it not
    a final merge, and an artifact that looks like an answer gets treated as one.
    """

    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for name, content in (
        ("base.md", comparison.base),
        ("local.md", comparison.local),
        ("remote.md", comparison.remote),
    ):
        if content is None:
            continue
        (output_dir / name).write_text(content, encoding="utf-8")
        written[name] = str(output_dir / name)

    report = {
        "schema": WORKSPACE_SCHEMA,
        **{key: value for key, value in compare_payload(comparison).items() if key != "next_actions"},
        "files": written,
        "next_actions": [
            {
                "label": "record the reconciled body against the remote it was merged with",
                "argv": [
                    "confluence",
                    "page",
                    "md",
                    "record-reconciled-against",
                    comparison.page_id,
                    "--md-file",
                    str(managed_path),
                    "--reconciled-file",
                    str(output_dir / "reconciled.md"),
                    "--compare-fingerprint",
                    comparison.fingerprint(),
                    "--format=json",
                ],
                "requires_user_approval": False,
            }
        ],
    }
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _atomic_replace(path: Path, text: str) -> None:
    """Temp file, fsync, rename. §7.4 step 7.

    A canonical document half-written is worse than one not written: the manifest and
    the body would disagree, and the next command would read a file that never existed
    as a whole.

    The temporary name is unique per attempt. It used to be a fixed
    `<name>.atls.tmp`, which two concurrent writers shared -- so one of them renamed
    the file out from under the other and the loser got a bare `FileNotFoundError`
    instead of the named refusal §7.4 requires. Review R2 reproduced that with a
    barrier; the lock below is what makes it unreachable, and a unique name is what
    keeps a bug in the lock from turning into that exception again.
    """

    temporary = path.with_name(f"{path.name}.atls.{os.getpid()}.{uuid4().hex[:8]}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _held(path: Path) -> Iterator[None]:
    """An exclusive lock on one canonical document, leaving nothing behind.

    Everything between re-deriving the fingerprint and replacing the file has to be
    inside this. Without it both processes pass the equality check and both go on to
    write: an atomic rename prevents a torn file, and does not make a
    compare-and-swap. Review R2 found that window and reproduced it with two real
    processes.

    Non-blocking on purpose. A writer that cannot take the lock is told
    `record_in_progress` rather than queued, because the comparison it holds was made
    before the other writer's change and will not be valid after it -- waiting would
    only delay the refusal it has to get anyway.

    **The lock is the canonical file itself, not a file beside it.** The first version
    created `<name>.atls.lock` and never removed it, so every record AND every refusal
    left a permanent artefact in a directory somebody keeps in Git -- which review R2
    called out against §7.4's "exactly one file" and §10.1's "the inline manifest is the
    only required persistent metadata". A caller that was refused must not change the
    working tree at all.

    `flock` is on the open file description, so two openers exclude each other whether
    they are threads or processes. Both writers open the document before either replaces
    it, so they hold the same inode and the exclusion is real; after the winner renames,
    a later process opens the new inode and locks that, which is correct because the
    winner has finished.

    Windows cannot use that, for a reason that matters: `msvcrt` locks byte ranges
    *mandatorily* and an open handle blocks a replace, so locking the canonical file there
    would block the very rename it exists to protect.

    So Windows uses a **named mutex**, which is the object Windows has for this and which
    avoids both problems the file-based attempts had:

    * the kernel releases it when the handle closes, including when the process dies, so
      there is no stale state and nothing depends on a later deletion;
    * the name is derived from the document's resolved path and nothing else, so every
      process that can write the document arbitrates on the same object.

    That second point is a correction. The previous version byte-locked a sentinel under
    `tempfile.gettempdir()`, and review R2 found that `gettempdir()` reads `TMP`/`TEMP`
    from the process environment: two processes with different `TMP` would lock two
    different sentinels and neither would see the other. §7.4's protected resource is the
    canonical document, so a caller's environment must not be able to move the arbitration
    domain. A mutex name has no such dependency, and nothing is left on disk anywhere.

    `Global\\` rather than `Local\\`: a shared checkout can be written from more than one
    logon session, and a lock that arbitrates only within a session would silently not
    arbitrate across them. Creating a `Global\\` mutex needs no privilege.

    `WAIT_ABANDONED` is accepted as ownership, which is the recovery neither file-based
    version could offer: the previous holder died without releasing and we now own it.

    **Still unverified.** This branch has never executed here, and a Linux green result
    cannot close AC7. That is a release gate, carried as a blocker rather than argued away.
    """

    if os.name == "nt":  # pragma: no cover - exercised on the Windows CI leg
        import ctypes
        from ctypes import wintypes

        # Derived from the document and nothing else: no environment variable, no
        # temporary directory, no working directory. R2's counterexample was exactly a
        # coordinate the caller could move.
        digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
        name = f"Global\\atls-record-{digest}"

        # `WinDLL` exists only on Windows, which is why this whole branch is guarded.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            raise ValidationError(
                "Could not create the record lock for this document.",
                context={"reason": "record_lock_unavailable", "path": str(path)},
            )
        try:
            wait_object_0, wait_abandoned = 0x0, 0x80
            # 0 means do not wait, for the same reason `LOCK_NB` does below.
            waited = kernel32.WaitForSingleObject(handle, 0)
            if waited not in (wait_object_0, wait_abandoned):
                raise ValidationError(
                    "Another process is recording against this document.",
                    hint="Compare again once it has finished; the comparison you hold is older than its write.",
                    context={"reason": "record_in_progress", "path": str(path)},
                )
            try:
                yield
            finally:
                kernel32.ReleaseMutex(handle)
        finally:
            kernel32.CloseHandle(handle)
        return

    import fcntl

    handle = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValidationError(
                "Another process is recording against this document.",
                hint="Compare again once it has finished; the comparison you hold is older than its write.",
                context={"reason": "record_in_progress", "path": str(path)},
            ) from error
        yield
    finally:
        # No unlink: there is nothing to unlink. Closing the descriptor releases the
        # lock, and the operating system closes it on a crash.
        os.close(handle)


def record_reconciled_against(
    client: Any,
    page_id: str,
    managed_path: Path,
    reconciled_path: Path,
    *,
    compare_fingerprint: str,
    base_file: Path | None = None,
) -> dict[str, Any]:
    """Rebind a reconciled body to the remote it was reconciled against. §7.4.

    PUT 0. This is the only step in the stale flow that may replace a canonical body,
    and it does exactly one thing to exactly one file.

    The fingerprint is re-derived from a fresh read rather than trusted. Two processes
    racing on the same document therefore end with one success and one named refusal,
    because the loser's fingerprint no longer describes the file it is about.
    """

    # The whole verify-then-replace sequence is inside the lock. Deriving the
    # fingerprint outside it and writing inside would leave exactly the window review
    # R2 exploited: both processes agree the page has not moved, and then both write.
    with _held(managed_path):
        return _record_under_lock(
            client,
            page_id,
            managed_path,
            reconciled_path,
            compare_fingerprint=compare_fingerprint,
            base_file=base_file,
        )


def _record_under_lock(
    client: Any,
    page_id: str,
    managed_path: Path,
    reconciled_path: Path,
    *,
    compare_fingerprint: str,
    base_file: Path | None,
) -> dict[str, Any]:
    fresh = compare(client, page_id, managed_path, base_file=base_file)
    observed = fresh.fingerprint()
    if observed != compare_fingerprint:
        # Which side moved, read off the two halves rather than guessed. If the local
        # half still matches, the remote is what changed, and vice versa; if neither
        # does, both did and the local one is reported because it is the one the caller
        # can look at.
        expected_remote, _, expected_local = compare_fingerprint.partition(".local:")
        reason = (
            "remote_changed_since_compare"
            if expected_local.strip() == fresh.local_fingerprint()
            and expected_remote != f"remote:{fresh.remote_fingerprint()}"
            else "local_changed_since_compare"
        )
        raise ValidationError(
            "The document or the page moved since the comparison this record is for.",
            hint=(
                "Compare again and re-read the two diffs. Recording against a comparison "
                "that no longer holds writes a body reconciled with something that is gone."
            ),
            context={
                "reason": reason,
                "page_id": page_id,
                "expected_fingerprint": compare_fingerprint,
                "observed_fingerprint": observed,
                "remote_version": fresh.remote_version,
            },
        )

    reconciled = reconciled_path.read_text(encoding="utf-8")
    if reconciled.lstrip().startswith("<!-- atls:managed"):
        raise ValidationError(
            "The reconciled file must be a plain Markdown body, not a managed document.",
            context={"reason": "reconciled_file_is_managed", "path": str(reconciled_path)},
        )
    if not reconciled.strip():
        raise ValidationError(
            "The reconciled file is empty.",
            context={"reason": "reconciled_file_empty", "path": str(reconciled_path)},
        )

    before_body = canonical_content_sha256(fresh.local)
    manifest = ManagedManifest(
        v=CURRENT_MANAGED_MANIFEST_VERSION,
        page=page_id,
        site=fresh.manifest.site,
        # The remote this body was reconciled against, re-read a moment ago and proven
        # unchanged by the fingerprint.
        remote_version=fresh.remote_version,
        remote_storage=fresh.remote_storage_sha256,
        # hash(R), never hash(the reconciled body). §5.1: `base_md` is always the hash
        # of a remote projection.
        base_md=canonical_content_sha256(fresh.remote),
        assets=fresh.manifest.assets,
        converter=f"cfxmark/{cfxmark.__version__}",
        profile=fresh.manifest.profile,
        passthrough=fresh.manifest.passthrough,
        authority=fresh.manifest.authority,
    )
    replacement = serialize_managed_manifest(manifest) + "\n" + reconciled
    _atomic_replace(managed_path, replacement)

    return {
        "schema": RECORD_SCHEMA,
        "status": "reconciled",
        "page_id": page_id,
        "path": str(managed_path),
        "remote_version": fresh.remote_version,
        "remote_storage_sha256": fresh.remote_storage_sha256,
        "base_md": manifest.base_md,
        # Before and after, §7.4 step 8. A receipt that says only "done" cannot be
        # audited afterwards, and this step is the one that overwrites somebody's file.
        "body_sha256_before": before_body,
        "body_sha256_after": canonical_content_sha256(reconciled),
        "remote_put_count": 0,
        "next_actions": [
            {
                "label": "check the reconciled document against the page it will be published to",
                "argv": [
                    "confluence",
                    "page",
                    "md",
                    "push",
                    page_id,
                    "--md-file",
                    str(managed_path),
                    "--dry-run",
                    "--format=json",
                ],
                "requires_user_approval": False,
            }
        ],
    }


def rebaseline(
    client: Any,
    page_id: str,
    managed_path: Path,
    *,
    accept_remote_baseline: str,
    base_file: Path | None = None,
) -> dict[str, Any]:
    """Move the baseline to the current remote, leaving the body alone. §5.4.

    The narrow escape from `manifest_base_projection_mismatch`: everything about the
    binding checks out and the recorded base hash is still not what this converter
    produces. That is not a missing base and must not be treated as one, so the only
    way out is here, with an explicit fingerprint the caller had to have seen.

    PUT 0, and the local body is untouched. The first push afterwards performs the
    full candidate, identity and ownership proof with no waiver, which is why moving
    the baseline is safe to allow at all.
    """

    # Same window, same lock. This one moves a baseline rather than a body, and a
    # baseline written against a page that moved between the check and the write is
    # exactly as wrong as a body would be.
    with _held(managed_path):
        return _rebaseline_under_lock(
            client, page_id, managed_path, accept_remote_baseline=accept_remote_baseline, base_file=base_file
        )


def _rebaseline_under_lock(
    client: Any,
    page_id: str,
    managed_path: Path,
    *,
    accept_remote_baseline: str,
    base_file: Path | None,
) -> dict[str, Any]:
    fresh = compare(client, page_id, managed_path, base_file=base_file)
    observed = fresh.fingerprint()
    if observed != accept_remote_baseline:
        raise ValidationError(
            "The approval fingerprint does not match the page as it now is.",
            context={
                "reason": "rebaseline_fingerprint_mismatch",
                "expected_fingerprint": accept_remote_baseline,
                "observed_fingerprint": observed,
            },
        )

    before = fresh.manifest
    manifest = ManagedManifest(
        v=CURRENT_MANAGED_MANIFEST_VERSION,
        page=page_id,
        site=before.site,
        remote_version=fresh.remote_version,
        remote_storage=fresh.remote_storage_sha256,
        base_md=canonical_content_sha256(fresh.remote),
        assets=before.assets,
        converter=f"cfxmark/{cfxmark.__version__}",
        profile=before.profile,
        passthrough=before.passthrough,
        authority=before.authority,
    )
    body = fresh.canonical_text.partition("\n")[2]
    _atomic_replace(managed_path, serialize_managed_manifest(manifest) + "\n" + body)

    return {
        "schema": REBASELINE_SCHEMA,
        "status": "rebaselined",
        "page_id": page_id,
        "path": str(managed_path),
        "remote_put_count": 0,
        "manifest_sha256_before": _sha256(serialize_managed_manifest(before)),
        "manifest_sha256_after": _sha256(serialize_managed_manifest(manifest)),
        # Unchanged, and said so. The point of this command is that it does not touch
        # the body, and a receipt that did not say so would leave that to be assumed.
        "body_sha256_before": canonical_content_sha256(body),
        "body_sha256_after": canonical_content_sha256(body),
        "base_md_before": before.base_md,
        "base_md_after": manifest.base_md,
    }


__all__ = [
    "COMPARE_SCHEMA",
    "FINGERPRINT_SCHEMA",
    "REBASELINE_SCHEMA",
    "RECORD_SCHEMA",
    "WORKSPACE_SCHEMA",
    "Comparison",
    "compare",
    "compare_payload",
    "rebaseline",
    "record_reconciled_against",
    "write_workspace",
]
