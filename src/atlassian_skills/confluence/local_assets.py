"""Resolve the images a Markdown file points at, before anything is uploaded.

Writing `![](diagram.png)` in a file and having it appear on the page is the
thing people expect a Markdown workflow to do. Until now it silently did not:
`page create` and `page update` had no notion of a local file at all, so the
reference published as a broken link and nothing said so.

This module works out *what would be uploaded*, and refuses anything it cannot
account for. It performs no uploads -- planning and doing are separate so a dry
run can show the plan, and so the refusals below happen before a single byte
reaches the server.

Four rules, each of them a way this goes wrong:

**A name is not an identity.** Two directories can both hold `diagram.png` and
mean different pictures. Content hash decides what is the same file, so the same
picture referenced twice uploads once and two different pictures with one name
are not silently merged.

**The base directory is a boundary, checked after resolving links.** `../../etc`
is the obvious escape and a symlink is the quiet one, so the check is on the real
path, not on the text of the reference.

**Already there means already there.** An attachment whose content hash matches
one on the page is reused, not re-uploaded. Re-uploading would create a new
version of an unchanged file on every publish.

**Nothing remote is ever deleted.** A reference disappearing from the Markdown
means the document stopped pointing at a picture, not that the picture should
go -- something else may link to it, and an attachment nobody can restore is a
worse outcome than one nobody references.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from atlassian_skills.core.errors import ValidationError

#: Markdown image references. Confluence-side references (`cfx:` markers, absolute
#: URLs) are left alone -- those already point at something that exists.
_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<src>[^)\s]+)(?:\s+\"[^\"]*\")?\)")

#: Refused rather than uploaded. A Markdown image reference that is not an image
#: is a mistake worth surfacing, and guessing a content type for the server to
#: reject later moves the error somewhere less useful.
ALLOWED_MEDIA = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml", "image/bmp", "image/tiff"}
)

#: Confluence installations cap attachment size themselves, and the cap varies.
#: This one exists so a mistyped path pointing at a disk image fails here, with
#: the filename in the message, rather than after a long upload.
MAX_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class LocalAsset:
    """One local file a Markdown document points at."""

    #: The reference exactly as written, so a rewrite can find it again.
    reference: str
    path: Path
    #: The name it will carry on the page. Confluence stores attachments in one
    #: flat namespace per page, so this is a basename and collisions matter.
    filename: str
    sha256: str
    media_type: str
    size: int


@dataclass(frozen=True)
class AssetUploadPlan:
    """What a publish would upload, reuse, and leave alone."""

    upload: tuple[LocalAsset, ...]
    reuse: tuple[LocalAsset, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "upload": [
                {"filename": item.filename, "sha256": item.sha256, "size": item.size, "media_type": item.media_type}
                for item in self.upload
            ],
            "reuse": [{"filename": item.filename, "sha256": item.sha256} for item in self.reuse],
            # Stated rather than implied. A caller reading a plan with no delete
            # key might reasonably wonder whether deletes are hidden elsewhere.
            "delete": [],
        }


def _code_line_numbers(markdown: str) -> frozenset[int]:
    """The 1-based lines that are code, taken from a real Markdown tokenizer.

    R4-pre found three separate corruptions in the hand-written line scanner this replaces,
    one per review round: nested fence width and delimiter semantics, indentation column
    semantics, and container prefixes. Each could expose a fenced example as a live image
    reference or hide a real one, on the path that rewrites the author's file.

    A fourth regular expression would have been another partial Markdown parser with no
    completeness boundary. So the ranges come from `mistletoe`, which cfxmark already parses
    every document with: it knows fence widths, indentation columns and container prefixes by
    construction, and it is the same reader that decides what the document *means* everywhere
    else in this stack.
    """

    from mistletoe import Document, block_token

    try:
        document = Document(markdown.splitlines(keepends=True))
    except Exception:  # noqa: BLE001 - an unparseable document has no code we can locate
        return frozenset()

    lines: set[int] = set()

    def walk(token: object) -> None:
        first = getattr(token, "line_number", 0) or 0
        body = len((getattr(token, "content", "") or "").splitlines())
        if isinstance(token, block_token.CodeFence):
            # Opener, body, closer. A fence left unclosed at end of document simply names
            # one line past the end, which the caller ignores.
            lines.update(range(first, first + body + 2))
        elif isinstance(token, block_token.BlockCode):
            lines.update(range(first, first + body))
        for child in getattr(token, "children", None) or []:
            walk(child)

    walk(document)
    return frozenset(lines)


def code_blanked(markdown: str) -> str:
    """The document with code spans and blocks blanked, character counts preserved.

    An image reference inside code is a document *showing* someone a reference, not making
    one. Found on a live corpus page documenting the image workflow: its fenced example named
    `assets/diagram.png`, no such file existed or was meant to, and the publish was refused
    for a picture nobody had asked to publish.

    Lengths are preserved rather than lines removed, so every match offset in the original
    still lines up -- `rewrite_references` substitutes into the real text and would corrupt
    the document if the two views disagreed about where anything is.
    """

    code_lines = _code_line_numbers(markdown)
    out: list[str] = []
    for number, line in enumerate(markdown.splitlines(keepends=True), start=1):
        stripped = line.rstrip("\r\n")
        newline = line[len(stripped) :]
        if number in code_lines:
            out.append(" " * len(stripped) + newline)
            continue
        # Inline code last: a backtick span on an ordinary line hides a reference too, and it
        # is the one case the block tokenizer gives no line range for.
        out.append(re.sub(r"`[^`\n]*`", lambda m: " " * len(m.group(0)), stripped) + newline)
    blanked = "".join(out)
    if len(blanked) != len(markdown):
        # A typed refusal, not an `assert`. This sits on the path that rewrites the author's
        # file, and `python -O` removes assertions -- so the one build where the invariant
        # matters most is the build without it.
        raise ValidationError(
            "Internal: the code-blanked view of the document lost its alignment",
            hint="This is a converter defect. The document was not modified.",
            context={
                "reason": "code_blanking_offset_mismatch",
                "source_length": len(markdown),
                "blanked_length": len(blanked),
            },
        )
    return blanked


def _is_remote(src: str) -> bool:
    return bool(urlsplit(src).scheme) or src.startswith("cfx:")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_local_assets(markdown: str, *, base_dir: Path) -> tuple[LocalAsset, ...]:
    """Every local image the document points at, resolved against `base_dir`.

    Raises rather than skipping. A reference that cannot be resolved is a broken
    picture on the published page, and publishing it quietly is how a document
    ends up with holes nobody notices until someone reads it.
    """

    base = base_dir.resolve()
    seen: dict[str, LocalAsset] = {}
    assets: list[LocalAsset] = []

    # Matched against the blanked view so an example inside code is not a reference,
    # and read back out of the original -- the offsets are identical by construction.
    for match in _IMAGE_RE.finditer(code_blanked(markdown)):
        src = match.group("src")
        if _is_remote(src):
            continue
        reference = unquote(src)
        candidate = (base / reference).resolve()

        # Resolved first, then compared. `..` is the obvious escape and a symlink
        # is the quiet one; only the real path catches both.
        if not candidate.is_relative_to(base):
            raise ValidationError(
                f"Image reference points outside the asset directory: {reference}",
                hint="Move the file under the asset directory, or pass --asset-dir to widen the base.",
                # `reference` is what the author typed and is the only way to find the
                # line to fix. The RESOLVED absolute path is not: it is base + reference,
                # which the caller already holds, and it puts this machine's directory
                # layout and account name into a JSON envelope that ends up in an agent
                # transcript. The reason code already says the reference escaped the base;
                # printing where it landed adds nothing to act on. Same at every raise
                # below. See `test_managed_error_redaction`, which now covers this module.
                context={"reason": "asset_outside_base", "reference": reference},
            )
        if not candidate.is_file():
            raise ValidationError(
                f"Image reference does not exist: {reference}",
                hint="Publishing it would leave a broken image on the page.",
                context={"reason": "asset_missing", "reference": reference},
            )

        size = candidate.stat().st_size
        if size > MAX_BYTES:
            raise ValidationError(
                f"Image is larger than the {MAX_BYTES // (1024 * 1024)} MiB limit: {reference}",
                context={"reason": "asset_too_large", "reference": reference, "size": size},
            )
        media_type = mimetypes.guess_type(candidate.name)[0] or ""
        if media_type not in ALLOWED_MEDIA:
            raise ValidationError(
                f"Image reference is not an image type we upload: {reference}",
                hint=f"Allowed: {', '.join(sorted(ALLOWED_MEDIA))}",
                context={"reason": "asset_media_type", "reference": reference, "media_type": media_type or "unknown"},
            )

        digest = file_sha256(candidate)
        existing = seen.get(digest)
        if existing is not None:
            # The same picture referenced twice uploads once, and keeps the name
            # it was first given so both references resolve to one attachment.
            assets.append(
                LocalAsset(
                    reference=reference,
                    path=existing.path,
                    filename=existing.filename,
                    sha256=digest,
                    media_type=existing.media_type,
                    size=existing.size,
                )
            )
            continue

        filename = candidate.name
        # Confluence keeps one flat attachment namespace per page, so two
        # different pictures called `diagram.png` would overwrite each other.
        # Distinguish by content, since content is what makes them different.
        colliding = {item.filename for item in seen.values()}
        if filename in colliding:
            stem, suffix = candidate.stem, candidate.suffix
            filename = f"{stem}-{digest[:8]}{suffix}"

        asset = LocalAsset(
            reference=reference,
            path=candidate,
            filename=filename,
            sha256=digest,
            media_type=media_type,
            size=size,
        )
        seen[digest] = asset
        assets.append(asset)

    return tuple(assets)


def plan_uploads(
    assets: tuple[LocalAsset, ...],
    remote_hashes: dict[str, str],
) -> AssetUploadPlan:
    """Split what must be uploaded from what the page already holds.

    `remote_hashes` maps an attachment filename to its content hash. Matching on
    content rather than name means an unchanged picture does not gain a new
    version on every publish, and a changed one under the same name does.
    """

    upload: list[LocalAsset] = []
    reuse: list[LocalAsset] = []
    planned: set[str] = set()
    for asset in assets:
        if asset.sha256 in planned:
            continue
        planned.add(asset.sha256)
        if remote_hashes.get(asset.filename) == asset.sha256:
            reuse.append(asset)
        else:
            upload.append(asset)
    return AssetUploadPlan(upload=tuple(upload), reuse=tuple(reuse))


def rewrite_references(markdown: str, assets: tuple[LocalAsset, ...]) -> str:
    """Point every local reference at the attachment it will become.

    Done on the exact reference text that was matched, so a document mixing local
    files, absolute URLs and Confluence markers only has its local files touched.
    """

    by_reference = {asset.reference: asset for asset in assets}

    def replace(match: re.Match[str]) -> str:
        src = match.group("src")
        asset = by_reference.get(unquote(src))
        return match.group(0) if asset is None else f"![{match.group('alt')}]({asset.filename})"

    # Rewritten by offset from the blanked view, for the same reason: a fenced example
    # must come out of this untouched. Substituting on the raw text would rewrite the
    # example into a page reference and change what the document teaches.
    blanked = code_blanked(markdown)
    pieces: list[str] = []
    cursor = 0
    for match in _IMAGE_RE.finditer(blanked):
        pieces.append(markdown[cursor : match.start()])
        pieces.append(replace(match))
        cursor = match.end()
    pieces.append(markdown[cursor:])
    return "".join(pieces)


__all__ = [
    "recover_assets",
    "ALLOWED_MEDIA",
    "AssetUploadInterrupted",
    "PreparedAssets",
    "prepare_assets",
    "UploadOutcome",
    "reusable_hashes",
    "upload_assets",
    "MAX_BYTES",
    "AssetUploadPlan",
    "LocalAsset",
    "file_sha256",
    "plan_uploads",
    "resolve_local_assets",
    "rewrite_references",
]


@dataclass(frozen=True)
class UploadOutcome:
    """What actually reached the page, and what may be stranded."""

    uploaded: tuple[str, ...]
    reused: tuple[str, ...]
    #: Files uploaded before a later step failed. They are on the page and the
    #: body does not reference them. Reported rather than cleaned up: deleting an
    #: attachment is not reversible, and a retry will reuse these by hash.
    orphaned: tuple[str, ...] = ()


def reusable_hashes(client: Any, page_id: str) -> dict[str, str]:
    """Content hashes of attachments already on the page -- almost always empty.

    Confluence does not expose a content hash for an attachment. The metadata it
    returns is a name, a media type and a byte count, and none of those prove two
    files are the same.

    So this reuses nothing by default, and every referenced image is uploaded.
    That costs a redundant attachment version. The alternative costs correctness:
    skipping an upload because the name and size match would leave the old
    picture on the page while the document claims the new one, and nothing would
    say so. A version nobody needed is visible in the history; a stale image is
    not.

    Kept as a seam rather than dropped. A caller that *does* hold hashes -- the
    managed workflow records them in its manifest -- can pass them to
    `plan_uploads` directly and get the reuse this cannot.
    """

    return {}


def stored_attachment_ids(client: Any, page_id: str) -> dict[str, str]:
    """`{filename: attachment id}` for what the page already holds.

    Not about reuse -- `reusable_hashes` answers that, and answers "nothing". This
    answers a different question: *how* to upload a name that is already there.

    An entry whose id did not come back is left out: without an id there is no version
    endpoint to post to, so that file goes to the create endpoint, which is what
    happened before this existed. Presence is a different question and must not be
    read from here -- see `recover_assets`.
    """

    stored: dict[str, str] = {}
    for item in client.list_attachments(page_id) or ():
        title = getattr(item, "title", None) or (item.get("title") if isinstance(item, dict) else None)
        identifier = getattr(item, "id", None) or (item.get("id") if isinstance(item, dict) else None)
        if title and identifier:
            stored[str(title)] = str(identifier)
    return stored


def _stored_ids_or_none_known(client: Any, page_id: str) -> dict[str, str]:
    """`stored_attachment_ids`, or an empty map if the page's list cannot be read.

    The list is an optimisation here, not a verdict: without it every file goes to the
    create endpoint, which is exactly what this path did before and is still correct for
    a name the page does not hold. A name it does hold is then answered `400` and
    reported as an interrupted upload -- so the failure surfaces at the next step rather
    than being hidden.

    Raising instead costs more than it buys, and the create path is where that shows:
    it uploads *after* the page exists, catches only `AssetUploadInterrupted`, and is
    documented to always report the page it just made. An unreadable attachment list
    escaping from here left a created page with its id known only to the server.

    `recover_assets` is the opposite case and must keep refusing: there the list decides
    which files are missing, so an unreadable one makes the answer unknowable.
    """

    try:
        return stored_attachment_ids(client, page_id)
    except Exception:  # noqa: BLE001 - an unreadable list degrades to "create", never to a stop
        return {}


def upload_assets(
    client: Any,
    page_id: str,
    plan: AssetUploadPlan,
    *,
    stored: Mapping[str, str] | None = None,
) -> UploadOutcome:
    """Put the planned files on the page, stopping at the first failure.

    Stops rather than continuing so the caller learns which files are already up
    when something goes wrong. Continuing would strand more of them and report
    one error at the end.

    A name the page already holds is posted to that attachment's own data endpoint,
    which adds a version. Sending it to the create endpoint instead -- what this did
    until measured -- makes Confluence answer `400 Cannot add a new attachment with
    same file name as an existing attachment`, so the *second* publish of any
    document carrying a picture failed there and took the body update with it: the
    page stayed on its old version while the pictures were already fine.
    `upload_attachments_batch` had found and fixed this; this path had not, and it is
    the one `page update`, `page create` and `push-md` all use.

    Uploading again rather than reusing is deliberate, not a fallback -- see
    `reusable_hashes`. This only makes "again" mean what Server/DC can actually do.

    `stored` maps a filename on the page to its attachment id. A caller that has just
    read the list passes it rather than paying for a second read; otherwise the list
    is read here, once, and only when there is something to upload.
    """

    if not plan.upload:
        return UploadOutcome(uploaded=(), reused=tuple(item.filename for item in plan.reuse))

    existing = dict(stored) if stored is not None else _stored_ids_or_none_known(client, page_id)
    uploaded: list[str] = []
    for asset in plan.upload:
        try:
            client.upload_attachment(
                page_id,
                asset.path,
                filename=asset.filename,
                attachment_id=existing.get(asset.filename),
            )
        except Exception as error:  # noqa: BLE001 - the caller decides what a partial upload means
            raise AssetUploadInterrupted(uploaded=tuple(uploaded), failed=asset.filename) from error
        uploaded.append(asset.filename)
    return UploadOutcome(uploaded=tuple(uploaded), reused=tuple(item.filename for item in plan.reuse))


class AssetUploadInterrupted(Exception):
    """An upload failed partway, and some files are already on the page.

    Carries what got through so the caller can say so. A retry reuses them by
    content hash rather than uploading again, and nothing is deleted -- an
    attachment removed cannot be restored, and something else may link to it.
    """

    def __init__(self, *, uploaded: tuple[str, ...], failed: str) -> None:
        super().__init__(f"upload failed at {failed} after {len(uploaded)} file(s)")
        self.uploaded = uploaded
        self.failed = failed


@dataclass(frozen=True)
class PreparedAssets:
    """A document with its image references pointed at attachments, and the plan."""

    markdown: str
    plan: AssetUploadPlan
    assets: tuple[LocalAsset, ...]

    @property
    def has_work(self) -> bool:
        return bool(self.assets)


def prepare_assets(
    markdown: str,
    *,
    base_dir: Path | None,
    remote: dict[str, str] | None = None,
) -> PreparedAssets:
    """Resolve a document's local images and rewrite it to reference them.

    `base_dir` is where relative references resolve from -- the Markdown file's
    own directory, unless the caller widened it. It may be `None` only when the
    document came from somewhere with no directory of its own, such as standard
    input; a document that references a local file from there is refused rather
    than resolved against the current working directory, which is whatever
    happened to be the shell's when the command ran.
    """

    if base_dir is None:
        stray = [match.group("src") for match in _IMAGE_RE.finditer(markdown) if not _is_remote(match.group("src"))]
        if stray:
            raise ValidationError(
                f"Local image reference with no directory to resolve it against: {stray[0]}",
                hint="Pass --asset-dir to say where these files are.",
                context={"reason": "asset_dir_required", "references": stray[:8]},
            )
        return PreparedAssets(markdown=markdown, plan=AssetUploadPlan((), ()), assets=())

    assets = resolve_local_assets(markdown, base_dir=base_dir)
    return PreparedAssets(
        markdown=rewrite_references(markdown, assets),
        plan=plan_uploads(assets, remote or {}),
        assets=assets,
    )


def recover_assets(client: Any, page_id: str, markdown: str, *, base_dir: Path | None) -> dict[str, Any]:
    """Upload the pictures a page is missing, and write no body.

    The recovery path for a create whose uploads did not all land. It has to be a
    separate command rather than a rerun of the write, because after a create the
    page body already *is* the candidate: the update path finds nothing to change,
    returns `no_change`, and never reaches the uploads. Measured -- the earlier
    advice to rerun the write recovered nothing at all.

    Missing means "no attachment with that filename". A name is not proof that
    two files are the same, and this does not claim it is: it claims the
    reference resolves, which is the thing that was broken. An upload is atomic,
    so a name that is present is a file that landed.

    Idempotent by consequence: run it twice and the second run uploads nothing.
    """

    if base_dir is None:
        raise ValidationError(
            "Recovering pictures needs the directory their references resolve against.",
            hint="Pass --asset-dir, or --body-file so the file's own directory can be used.",
            context={"reason": "asset_dir_required", "page_id": page_id},
        )
    assets = resolve_local_assets(markdown, base_dir=base_dir)
    if not assets:
        return {"status": "no_local_assets", "page_id": page_id, "uploaded": [], "already_present": []}

    try:
        # A name, not an id: this asks whether the reference resolves, and an
        # attachment whose id did not come back is still present. Deciding presence
        # from `stored_attachment_ids` instead would call a stored file missing and
        # upload it again -- measured against this module's own fake.
        present = {
            str(getattr(item, "title", None) or (item.get("title") if isinstance(item, dict) else ""))
            for item in (client.list_attachments(page_id) or [])
        }
    except Exception as error:  # noqa: BLE001 - an unreadable page is a refusal, not a silent upload
        raise ValidationError(
            "Could not read the page's attachments, so what is missing cannot be told.",
            context={"reason": "attachment_inventory_unavailable", "page_id": page_id},
        ) from error

    missing = tuple(asset for asset in assets if asset.filename not in present)
    already = sorted({asset.filename for asset in assets if asset.filename in present})
    if not missing:
        return {"status": "already_complete", "page_id": page_id, "uploaded": [], "already_present": already}

    # Every `missing` name is by definition absent from the page, so each one is a
    # create and needs no stored id. The empty map says so and skips a second read.
    outcome = upload_assets(client, page_id, plan_uploads(missing, remote_hashes={}), stored={})
    return {
        "status": "recovered",
        "page_id": page_id,
        "uploaded": list(outcome.uploaded),
        "already_present": already,
    }
