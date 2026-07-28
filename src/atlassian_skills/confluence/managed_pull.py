"""State-free managed Markdown pull preparation and publication."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import cfxmark

from atlassian_skills.confluence.asset_sync import (
    attachment_version,
    extract_managed_asset_references,
    rewrite_attachment_artifact,
)
from atlassian_skills.core.attachment_io import allocate_attachment_filename
from atlassian_skills.core.errors import ConflictError, ValidationError
from atlassian_skills.core.file_identity import inspect_file_identity
from atlassian_skills.core.managed_file import read_managed_utf8, resolve_managed_asset_path
from atlassian_skills.core.managed_manifest import (
    ManagedAssetRecord,
    ManagedDocument,
    ManagedManifest,
    ManagedManifestError,
    canonical_asset_set_sha256,
    canonical_content_sha256,
    extract_asset_records,
    parse_managed_document,
    serialize_asset_record,
    serialize_managed_manifest,
)
from atlassian_skills.core.publication import PublicationFile, publish_managed_files
from atlassian_skills.core.site_identity import site_fingerprint

_IMAGE_RE = re.compile(
    r"!\[(?:\\.|[^\]])*\]\((?P<target><(?:\\.|[^>])*>|(?:\\.|[^)])+)\)"
    r"(?P<img_metadata><!-- cfxmark:img(?: [^<>]*)? -->)?"
)


@dataclass(frozen=True)
class PreparedPortablePull:
    page_id: str
    title: str
    output_path: Path
    markdown: str
    version: int
    status: str
    warnings: tuple[str, ...]
    losses: tuple[str, ...]
    push_safe: bool
    blockers: tuple[dict[str, Any], ...]
    migration_report: dict[str, Any]
    migration_report_sha256: str
    asset_records: tuple[ManagedAssetRecord, ...]
    edit_guidance: tuple[dict[str, Any], ...]
    writes: tuple[PublicationFile, ...]


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _page_version(page: Any) -> int:
    version = getattr(page, "version", None)
    number = getattr(version, "number", version)
    return int(number or 1)


def _existing_document(output_path: Path, *, page_id: str, site: str) -> ManagedDocument | None:
    if not output_path.exists() and not output_path.is_symlink():
        return None
    try:
        inspect_file_identity(output_path)
        managed = read_managed_utf8(output_path)
        document = parse_managed_document(managed, assets=extract_asset_records(managed))
    except (ManagedManifestError, ValidationError) as error:
        reason = error.reason if isinstance(error, ManagedManifestError) else (error.context or {}).get("reason")
        raise ConflictError(
            "Output already exists and is not an unchanged portable managed file",
            context={"reason": "local_changes" if reason and "tampered" in str(reason) else "output_conflict"},
        ) from error
    if document.manifest.page != page_id or document.manifest.site != site:
        raise ConflictError(
            "Existing output targets a different Confluence page or site",
            context={"reason": "output_conflict"},
        )
    for asset in document.assets:
        if asset.materialization != "local":
            continue
        local_path = resolve_managed_asset_path(output_path, asset.src)
        try:
            inspect_file_identity(local_path)
            actual = _sha256(local_path.read_bytes())
        except (OSError, ValidationError) as error:
            raise ConflictError(
                "Managed asset is missing or unsafe",
                context={"reason": "local_asset_changes", "src": asset.src},
            ) from error
        if actual != asset.sha256:
            raise ConflictError(
                "Managed asset contains local edits and will not be overwritten",
                context={"reason": "local_asset_changes", "src": asset.src},
            )
    return document


def _portable_asset_src(output_path: Path, asset_dir: Path, filename: str) -> str:
    try:
        relative_dir = asset_dir.absolute().relative_to(output_path.parent.absolute())
    except ValueError as error:
        raise ValidationError(
            "Managed asset directory must be inside the Markdown file directory",
            context={"reason": "nonportable_asset_directory"},
        ) from error
    return (PurePosixPath(*relative_dir.parts) / filename).as_posix()


def _detached_portable_asset_dir(output_path: Path, *, page_id: str, site: str) -> Path:
    """Return the stable, page-bound local directory for hidden attachments.

    Detached records have no Markdown anchor and therefore need a path that
    does not depend on the caller's chosen Markdown filename.  Binding the
    directory to the page and site preserves fixed points without sharing an
    arbitrary sibling page's local asset file.
    """

    identity = hashlib.sha256(f"{site}\0{page_id}".encode()).hexdigest()
    return output_path.with_name(f".atls-detached-{identity}.assets")


def in_place_edit_guidance(prediction: str, codes: tuple[str, ...]) -> dict[str, Any] | None:
    """Value-free guidance for a pre-computed in-place edit prediction, or ``None``.

    Shared with ``page inspect``: the prediction is what decides whether an
    in-place Markdown edit is worth starting at all, so the two surfaces that
    answer that question must not drift apart in wording or in the codes they
    name. ``None`` means the prediction carries no in-place-specific advice and
    the caller falls back to its own default.
    """

    if prediction == "blocked":
        return {
            "kind": "in_place_blocked",
            "action": "append_or_patch_text",
            "message": (
                "An in-place Markdown edit of this page cannot be proven safe by the "
                "current converter and publishing would fail closed. Appending new "
                "blocks at EOF (exact-append) and page patch-text remain available."
            ),
            "codes": list(codes),
        }
    if prediction == "consent":
        return {
            "kind": "in_place_with_consent",
            "action": "edit_then_dry_run",
            "message": (
                "Edits are provable, but publishing re-renders remote-only details "
                "this Markdown cannot carry; push-md --dry-run will list them for "
                "explicit consent."
            ),
            "codes": list(codes),
        }
    return None


def _predict_in_place_editability(
    artifact: Any,
    storage: str,
    passthrough_prefixes: tuple[str, ...],
) -> tuple[str, tuple[str, ...]]:
    """Predict whether an in-place Markdown edit of this page can be proven.

    The ownership-proof outcome is edit-independent for every observed failure
    class — it is the S<->C0 round-trip correspondence itself that fails, so
    pages that will reject an in-place edit reject an *unedited* re-render
    identically. Running the managed proof once at pull time therefore tells
    the user before editing whether in-place edits are provable ("ready"),
    provable but gated on loss consent ("consent"), or currently blocked
    ("blocked" — exact-append and page patch-text remain available). Without
    this, a page can pull clean and only fail at push dry-run.
    """

    options = cfxmark.ConversionOptions(
        profile="editable",
        passthrough_html_comment_prefixes=passthrough_prefixes,
    )
    base = replace(artifact, protected_regions=(), remote_subtrees=())
    try:
        candidate = cfxmark.to_cfx_artifact(
            base.markdown,
            presentation=base.presentation,
            base_artifact=base,
            splice_source=storage,
            options=options,
        )
        cfxmark.validate_managed_cfx_artifact(candidate, source_storage=storage, options=options)
    except cfxmark.OwnershipProofError as error:
        summary = getattr(error, "summary", None)
        fatal_class = getattr(summary, "fatal_class", None)
        return "blocked", (fatal_class or "ownership_proof_invalid",)
    except (cfxmark.CfxmarkError, TypeError, ValueError) as error:
        from atlassian_skills.confluence.migration_preflight import conversion_failure_code

        return "blocked", (conversion_failure_code(error),)
    report = candidate.source_migration_report or artifact.migration_report
    consent_codes = tuple(
        sorted(
            {
                occurrence.code
                for occurrence in getattr(report, "occurrences", ())
                if str(occurrence.effect) in {"converted", "removed", "unsupported"}
            }
        )
    )
    if consent_codes:
        return "consent", consent_codes
    return "ready", ()


def _attach_asset_comments(
    markdown: str,
    records_by_reference: dict[str, ManagedAssetRecord],
    *,
    detached_records: tuple[ManagedAssetRecord, ...] = (),
) -> str:
    seen: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        target = match.group("target")
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        target = target.split("#cfxmark:", 1)[0]
        record = records_by_reference.get(target)
        if record is None:
            return match.group(0)
        seen.add(record.remote_name)
        return match.group(0) + serialize_asset_record(record)

    result = _IMAGE_RE.sub(replace, markdown)
    missing = sorted(record.remote_name for record in records_by_reference.values() if record.remote_name not in seen)
    if missing:
        raise ValidationError(
            "Managed attachment identity could not be bound to its Markdown image",
            context={"reason": "asset_marker_unresolved", "remote_names": missing},
        )
    if detached_records:
        # Keep detached records in the body prefix, immediately below the
        # managed manifest.  EOF belongs to user-authored Markdown: appending
        # there must not make the next pull move this registry across the new
        # content and break the managed fixed point.
        registry = "\n".join(serialize_asset_record(record) for record in detached_records)
        result = registry + "\n\n" + result
    return result


def _write_if_changed(
    writes: list[PublicationFile],
    *,
    path: Path,
    content: bytes,
    kind: str,
    existing_assets: dict[str, ManagedAssetRecord],
    asset_src: str | None = None,
) -> None:
    if path.exists() or path.is_symlink():
        try:
            inspect_file_identity(path)
            actual = path.read_bytes()
        except (OSError, ValidationError) as error:
            raise ConflictError(
                "Publication destination is unsafe",
                context={"reason": "publication_conflict", "path": str(path)},
            ) from error
        if actual == content:
            return
        if kind == "asset":
            prior = existing_assets.get(asset_src or "")
            if prior is None or _sha256(actual) != prior.sha256:
                raise ConflictError(
                    "Asset destination contains unrelated or locally edited content",
                    context={"reason": "asset_output_conflict", "src": asset_src},
                )
    writes.append(PublicationFile(destination=path, content=content, kind=kind))


def prepare_portable_pull(
    client: Any,
    page_id: str,
    output_path: Path,
    *,
    passthrough_prefixes: tuple[str, ...] = (),
    site_url: str | None = None,
    asset_dir: Path | None = None,
    no_assets: bool = False,
    page: Any | None = None,
) -> PreparedPortablePull:
    effective_site_url = site_url or getattr(client, "base_url", None)
    if not isinstance(effective_site_url, str):
        raise ValidationError("Portable managed pull requires the configured Confluence base URL")
    site = site_fingerprint(effective_site_url)
    existing = _existing_document(output_path, page_id=page_id, site=site)
    existing_assets = {asset.src: asset for asset in existing.assets} if existing is not None else {}

    if no_assets and asset_dir is not None:
        raise ValidationError(
            "--asset-dir cannot be combined with --no-assets",
            context={"reason": "asset_option_conflict"},
        )
    effective_asset_dir = asset_dir or output_path.with_name(f"{output_path.stem}.assets")

    page = page if page is not None else client.get_page(page_id)
    storage = page.body_storage or ""
    version = _page_version(page)
    artifact = cfxmark.to_md_artifact(
        storage,
        options=cfxmark.ConversionOptions(
            profile="editable",
            passthrough_html_comment_prefixes=passthrough_prefixes,
        ),
    )
    # Predict on the pre-rewrite artifact: this is exactly the input the push
    # proof sees, and asset-path rewriting would perturb the round-trip.
    # The prediction is advisory only, but it now runs the full ownership proof
    # *before* the file is written. An unexpected failure must not abort a pull
    # that would otherwise succeed — pull is the recovery path and has to keep
    # writing the file. Degrade to neutral guidance ("edit, then dry-run"),
    # which is what pull emitted before the prediction existed; never degrade to
    # a false "safe" signal, and never to a false "blocked" one.
    try:
        edit_preflight = _predict_in_place_editability(artifact, storage, passthrough_prefixes)
    except Exception:
        edit_preflight = ("unknown", ())
    records: list[ManagedAssetRecord] = []
    local_contents: list[tuple[Path, bytes, str]] = []
    references: dict[str, str] = {}
    visible_remote_names: set[str] = set()
    used_names: set[str] = set()
    if artifact.attachments:
        attachment_payloads: list[tuple[Any, bytes, str | None]] = []
        attachment_groups: dict[str, list[Any]] = {}
        for attachment in client.list_attachments(page_id):
            attachment_groups.setdefault(attachment.title, []).append(attachment)
        for remote_name in artifact.attachments:
            matches = attachment_groups.get(remote_name, [])
            if len(matches) != 1:
                raise ValidationError(
                    "Referenced attachment identity is missing or ambiguous",
                    context={"reason": "asset_identity_conflict", "filename": remote_name, "matches": len(matches)},
                )
            attachment = matches[0]
            download_link = attachment.links.download if getattr(attachment, "links", None) else None
            content = client.fetch_attachment_bytes(attachment.id, download_link)
            if no_assets:
                references[remote_name] = remote_name
                attachment_payloads.append((attachment, content, None))
            else:
                stored_name = allocate_attachment_filename(remote_name, attachment.id, used_names)
                references[remote_name] = _portable_asset_src(output_path, effective_asset_dir, stored_name)
                attachment_payloads.append((attachment, content, stored_name))

        if not no_assets:
            provisional_artifact = rewrite_attachment_artifact(artifact, references)
            visible_references = {
                reference.local_reference
                for reference in extract_managed_asset_references(provisional_artifact.managed_markdown)
            }
            visible_remote_names = {
                remote_name
                for remote_name, reference in references.items()
                if reference in visible_references or quote(reference, safe="/-._~") in visible_references
            }

        for attachment, content, stored_filename in attachment_payloads:
            remote_name = attachment.title
            if no_assets:
                src = remote_name
                materialization = "remote-only"
            else:
                materialization_dir = (
                    effective_asset_dir
                    if remote_name in visible_remote_names
                    else asset_dir or _detached_portable_asset_dir(output_path, page_id=page_id, site=site)
                )
                assert stored_filename is not None
                src = _portable_asset_src(output_path, materialization_dir, stored_filename)
                local_contents.append((resolve_managed_asset_path(output_path, src), content, src))
                materialization = "local"
            references[remote_name] = src
            record = ManagedAssetRecord(
                materialization=materialization,
                src=src,
                remote_id=str(attachment.id),
                remote_version=attachment_version(attachment),
                remote_name=remote_name,
                sha256=_sha256(content),
            )
            records.append(record)

    if references:
        artifact = rewrite_attachment_artifact(artifact, references)
    records_by_reference = {
        quote(references[record.remote_name], safe="/-._~"): record
        for record in records
        if record.remote_name in visible_remote_names
    }
    detached_records = tuple(record for record in records if record.remote_name not in visible_remote_names)
    managed_body = _attach_asset_comments(
        artifact.managed_markdown,
        records_by_reference,
        detached_records=detached_records,
    )
    asset_records = tuple(records)
    manifest = ManagedManifest(
        v=2,
        page=page_id,
        site=site,
        remote_version=version,
        remote_storage=(
            artifact.source_storage_sha256
            if artifact.source_storage_sha256.startswith("sha256:")
            else f"sha256:{artifact.source_storage_sha256}"
        ),
        base_md=canonical_content_sha256(managed_body),
        assets=canonical_asset_set_sha256(asset_records),
        converter=f"cfxmark/{cfxmark.__version__}",
        profile="markdown-first",
        passthrough=passthrough_prefixes,
    )
    managed = serialize_managed_manifest(manifest) + "\n" + managed_body
    parse_managed_document(managed, assets=extract_asset_records(managed))

    writes: list[PublicationFile] = []
    for local_path, content, src in local_contents:
        _write_if_changed(
            writes,
            path=local_path,
            content=content,
            kind="asset",
            existing_assets=existing_assets,
            asset_src=src,
        )
    _write_if_changed(
        writes,
        path=output_path,
        content=managed.encode("utf-8"),
        kind="markdown",
        existing_assets=existing_assets,
    )

    blockers = tuple(asdict(diagnostic) for diagnostic in artifact.diagnostics if diagnostic.blocking)
    warnings = tuple(diagnostic.message for diagnostic in artifact.diagnostics if not diagnostic.blocking)
    losses = tuple(diagnostic.message for diagnostic in artifact.diagnostics if diagnostic.blocking)
    needs_migration = artifact.status == "needs_migration"
    from atlassian_skills.confluence.migration_preflight import _migration_report_payload, _report_hash

    migration_report = _migration_report_payload(artifact.migration_report, display=True)
    predicted = in_place_edit_guidance(*edit_preflight)
    if predicted is not None:
        guidance: tuple[dict[str, Any], ...] = (predicted,)
    elif needs_migration:
        guidance = (
            {
                "kind": "full_migration",
                "action": "edit_then_dry_run",
                "message": "Review migration comments, edit normally, then run push-md --dry-run.",
            },
        )
    else:
        guidance = (
            {
                "kind": "proof_candidate",
                "action": "edit_then_dry_run",
                "message": "Edit normally, then run push-md --dry-run before publishing.",
            },
        )
    return PreparedPortablePull(
        page_id=page_id,
        title=page.title,
        output_path=output_path,
        markdown=managed,
        version=version,
        status="pulled_with_migrations" if needs_migration else "pulled",
        warnings=warnings,
        losses=losses,
        push_safe=artifact.push_safe,
        blockers=blockers,
        migration_report=migration_report,
        migration_report_sha256=_report_hash(artifact.migration_report),
        asset_records=asset_records,
        edit_guidance=guidance,
        writes=tuple(writes),
    )


def publish_portable_pulls(prepared: tuple[PreparedPortablePull, ...]) -> None:
    publish_managed_files(tuple(write for page in prepared for write in page.writes))


__all__ = [
    "PreparedPortablePull",
    "prepare_portable_pull",
    "publish_portable_pulls",
    "resolve_managed_asset_path",
]
