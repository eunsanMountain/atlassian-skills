"""Pull Confluence pages to Markdown with optional batched sidecar assets."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote

import cfxmark

from atlassian_skills.confluence.compatibility import compatibility_payload
from atlassian_skills.core.attachment_io import (
    AttachmentWriteBatch,
    allocate_attachment_filename,
    safe_attachment_filename,
)
from atlassian_skills.core.errors import ValidationError


class PullResult(NamedTuple):
    """Result from pull_md containing markdown content and page metadata."""

    markdown: str
    version: int
    title: str
    warnings: tuple[str, ...] = ()
    losses: tuple[str, ...] = ()
    push_safe: bool = True
    status: str = "ready_to_edit"
    blockers: tuple[dict[str, Any], ...] = ()
    migration_report: dict[str, Any] | None = None
    migration_report_sha256: str | None = None
    assets: tuple[dict[str, Any], ...] = ()
    edit_guidance: tuple[dict[str, Any], ...] = ()
    #: The `atls-compatibility-v1` assessment for the page as pulled, so the
    #: caller knows what kind of document it received before editing it rather
    #: than discovering it at push time.
    compatibility: dict[str, Any] = {}


class PullPageResult(NamedTuple):
    """One persisted page returned by a multi-page pull."""

    page_id: str
    title: str
    path: Path
    version: int
    assets: int
    warnings: tuple[str, ...] = ()
    losses: tuple[str, ...] = ()
    push_safe: bool = True
    status: str = "ready_to_edit"
    blockers: tuple[dict[str, Any], ...] = ()
    migration_report: dict[str, Any] | None = None
    migration_report_sha256: str | None = None


class _PendingPage(NamedTuple):
    page_id: str
    title: str
    path: Path
    markdown: str
    version: int
    assets: int
    warnings: tuple[str, ...]
    losses: tuple[str, ...]
    push_safe: bool


_ASSET_MARKER_RE = re.compile(
    r"(?P<prefix>!\[(?:\\.|[^\]])*\]\()"
    r"(?P<target><(?:\\.|[^>])*>|(?:\\.|[^)])+)\)"
    r"(?P<img_metadata><!-- cfxmark:img(?: [^<>]*)? -->)?"
    r'<!-- cfxmark:asset src="(?P<remote_filename>[^"]*)" -->'
)
_IMAGE_METADATA_RE = re.compile(
    r"cfxmark:(?:[wh]=\d+|thumbnail=1|align=(?:center|right))"
    r"(?:,(?:[wh]=\d+|thumbnail=1|align=(?:center|right)))*"
)


def _page_version(page: Any) -> int:
    if page.version is None:
        return 1
    if hasattr(page.version, "number"):
        return int(page.version.number)
    if isinstance(page.version, int):
        return page.version
    return 1


def _convert_storage(
    storage_body: str,
    passthrough_prefixes: list[str] | None,
) -> cfxmark.ConversionResult:
    from atlassian_skills.core.format.markdown import confluence_storage_to_md_result

    return confluence_storage_to_md_result(
        storage_body,
        profile="editable",
        passthrough_prefixes=tuple(passthrough_prefixes or ()),
    )


def safe_page_directory_name(title: str, page_id: str) -> str:
    """Build a Windows-safe, collision-resistant directory name for a page."""
    safe_title = safe_attachment_filename(title, f"page_{page_id}")[:120].rstrip(" .")
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", page_id)[:40] or "page"
    return f"{safe_title}--{safe_id}"


def _sidecar_link_base(asset_dir: Path, md_path: Path | None) -> str:
    if md_path is not None:
        try:
            relative = asset_dir.relative_to(md_path.parent).as_posix()
        except ValueError:
            relative = asset_dir.name
    else:
        relative = asset_dir.name
    return f"{relative}/"


def _rewrite_sidecar_links(
    md_content: str,
    replacements: dict[str, str],
    asset_dir: Path,
    md_path: Path | None,
) -> str:
    if not replacements:
        return md_content
    link_base = _sidecar_link_base(asset_dir, md_path)

    def replace_marker(match: re.Match[str]) -> str:
        stored_name = replacements.get(match.group("remote_filename"))
        if stored_name is None:
            return match.group(0)
        visible_target = match.group("target")
        metadata_match = _IMAGE_METADATA_RE.search(visible_target)
        metadata = metadata_match.group(0) if metadata_match else ""
        target = quote(f"{link_base}{stored_name}", safe="/-._~")
        if metadata:
            target = f"{target}#{metadata}"
        img_metadata = match.group("img_metadata") or ""
        return f"{match.group('prefix')}{target}){img_metadata}"

    return _ASSET_MARKER_RE.sub(replace_marker, md_content)


def _preflight_managed_asset_destination(
    *,
    local_path: Path,
    content: bytes,
    markdown_path: Path,
    existing_assets: dict[Path, Any],
) -> None:
    from atlassian_skills.core.file_identity import inspect_file_identity

    if local_path == markdown_path.resolve(strict=False):
        raise ValidationError(
            "Managed Markdown and sidecar asset destinations collide",
            context={"reason": "asset_output_collision", "path": str(local_path)},
        )
    if not local_path.exists():
        return
    try:
        inspect_file_identity(local_path)
        local_hash = hashlib.sha256(local_path.read_bytes()).hexdigest()
    except OSError as error:
        raise ValidationError(
            "Managed sidecar preflight could not read an existing destination",
            context={
                "reason": "asset_preflight_io_failed",
                "path": str(local_path),
                "failure": type(error).__name__,
            },
        ) from error
    desired_hash = hashlib.sha256(content).hexdigest()
    if local_hash == desired_hash:
        return
    baseline = existing_assets.get(local_path)
    if baseline is None or local_hash != baseline.content_sha256:
        raise ValidationError(
            "Managed sidecar asset contains local or unrelated content and will not be overwritten",
            hint="Choose a different --asset-dir or reconcile the local asset before pulling again.",
            context={"reason": "asset_output_conflict", "path": str(local_path)},
        )


def _stage_sidecar_assets(
    client: Any,
    page_id: str,
    md_content: str,
    asset_dir: Path,
    md_path: Path | None,
    batch: AttachmentWriteBatch,
) -> tuple[str, int]:
    markers = list(_ASSET_MARKER_RE.finditer(md_content))
    if not markers:
        return md_content, 0

    batch.bind_directory(asset_dir)
    attachments = client.list_attachments(page_id)
    attachment_map = {attachment.title: attachment for attachment in attachments}
    used_names: set[str] = set()
    replacements: dict[str, str] = {}

    for marker in markers:
        original_filename = marker.group("remote_filename")
        if original_filename in replacements:
            continue
        attachment = attachment_map.get(original_filename)
        if attachment is None:
            continue
        stored_name = allocate_attachment_filename(attachment.title, attachment.id, used_names)
        download_link = attachment.links.download if attachment.links else None
        content = client.fetch_attachment_bytes(attachment.id, download_link)
        batch.add(asset_dir / stored_name, content)
        replacements[original_filename] = stored_name

    if not replacements:
        return md_content, 0

    return _rewrite_sidecar_links(md_content, replacements, asset_dir, md_path), len(replacements)


def pull_md(
    client: Any,
    page_id: str,
    output_path: Path | None = None,
    *,
    passthrough_prefixes: list[str] | None = None,
    resolve_assets: str | None = None,
    asset_dir: Path | None = None,
    site_url: str | None = None,
    portable: bool = False,
    no_assets: bool = False,
    accept_migration: str | None = None,
    write_base_cache: bool = False,
) -> PullResult:
    """Pull one Confluence page and publish Markdown only after its assets succeed."""
    if portable:
        if output_path is None:
            raise ValidationError("Portable managed pull requires --output", context={"reason": "output_required"})
        if resolve_assets not in {None, "sidecar"}:
            raise ValidationError(
                "Unknown managed asset resolution mode",
                context={"reason": "invalid_asset_mode", "resolve_assets": resolve_assets},
            )
        from atlassian_skills.confluence.managed_pull import prepare_portable_pull, publish_portable_pulls
        from atlassian_skills.core.managed_manifest import (
            ManagedManifestError,
            parse_passthrough,
            serialize_passthrough,
        )

        try:
            canonical_prefixes = parse_passthrough(serialize_passthrough(passthrough_prefixes or ()))
        except ManagedManifestError as error:
            raise ValidationError("Invalid passthrough prefix", context=error.context) from error
        prepared = prepare_portable_pull(
            client,
            page_id,
            output_path,
            passthrough_prefixes=canonical_prefixes,
            site_url=site_url,
            asset_dir=asset_dir,
            no_assets=no_assets,
            accept_migration=accept_migration,
            write_base_cache=write_base_cache,
        )
        publish_portable_pulls((prepared,))
        return PullResult(
            markdown=prepared.markdown,
            version=prepared.version,
            title=prepared.title,
            warnings=prepared.warnings,
            losses=prepared.losses,
            push_safe=prepared.push_safe,
            status=prepared.status,
            blockers=prepared.blockers,
            migration_report=prepared.migration_report,
            migration_report_sha256=prepared.migration_report_sha256,
            compatibility=prepared.compatibility,
            assets=tuple(asdict(asset) for asset in prepared.asset_records),
            edit_guidance=prepared.edit_guidance,
        )
    if accept_migration is not None:
        raise ValidationError(
            "--accept-migration applies to the managed Markdown pull only",
            hint="Add --portable, or drop the approval: this path writes no manifest to approve against.",
            context={"reason": "approval_not_applicable"},
        )
    if resolve_assets is not None and resolve_assets != "sidecar":
        raise ValueError(f"Unknown resolve_assets mode: {resolve_assets!r} (expected 'sidecar')")

    batch = AttachmentWriteBatch()
    try:
        if output_path is not None:
            batch.bind_directory(output_path.parent)
        if resolve_assets == "sidecar" and asset_dir is not None:
            batch.bind_directory(asset_dir)
        page = client.get_page(page_id)
        conversion = _convert_storage(page.body_storage or "", passthrough_prefixes)
        md_content = conversion.markdown or ""
        if resolve_assets == "sidecar" and asset_dir is not None:
            md_content, _ = _stage_sidecar_assets(client, page_id, md_content, asset_dir, output_path, batch)
        if output_path is not None:
            batch.add(output_path, md_content.encode("utf-8"))
        batch.commit()
    except BaseException:
        batch.abort()
        raise

    return PullResult(
        markdown=md_content,
        version=_page_version(page),
        title=page.title,
        warnings=conversion.warnings,
        losses=conversion.losses,
        push_safe=conversion.push_safe,
        compatibility=compatibility_payload(page_id, page.body_storage or "", document_path=str(output_path)),
    )


def _resolve_assets_sidecar(
    client: Any,
    page_id: str,
    md_content: str,
    asset_dir: Path,
    md_path: Path | None = None,
) -> str:
    """Resolve one Markdown string through the common attachment batch primitive."""
    batch = AttachmentWriteBatch()
    try:
        rewritten, _ = _stage_sidecar_assets(client, page_id, md_content, asset_dir, md_path, batch)
        batch.commit()
        return rewritten
    except BaseException:
        batch.abort()
        raise


def pull_pages_batch(
    client: Any,
    page_ids: list[str],
    output_root: Path,
    *,
    passthrough_prefixes: list[str] | None = None,
    site_url: str | None = None,
    fault_hook: Any | None = None,
    portable: bool = False,
    no_assets: bool = False,
) -> list[PullPageResult]:
    """Pull several pages with one client and one cross-page attachment commit."""
    if not page_ids:
        raise ValidationError("At least one Confluence page ID is required")
    if len(set(page_ids)) != len(page_ids):
        raise ValidationError("Duplicate Confluence page IDs are not allowed in one pull batch")
    if portable:
        from atlassian_skills.confluence.managed_pull import prepare_portable_pull, publish_portable_pulls
        from atlassian_skills.core.managed_manifest import (
            ManagedManifestError,
            parse_passthrough,
            serialize_passthrough,
        )

        try:
            canonical_prefixes = parse_passthrough(serialize_passthrough(passthrough_prefixes or ()))
        except ManagedManifestError as error:
            raise ValidationError("Invalid passthrough prefix", context=error.context) from error
        prepared_pages = []
        for page_id in page_ids:
            page = client.get_page(page_id)
            page_directory = output_root / safe_page_directory_name(page.title, page_id)
            markdown_name = f"{safe_attachment_filename(page.title, f'page_{page_id}')[:120]}.md"
            prepared_pages.append(
                prepare_portable_pull(
                    client,
                    page_id,
                    page_directory / markdown_name,
                    passthrough_prefixes=canonical_prefixes,
                    site_url=site_url,
                    no_assets=no_assets,
                    page=page,
                )
            )
        publish_portable_pulls(tuple(prepared_pages))
        return [
            PullPageResult(
                page_id=prepared.page_id,
                title=prepared.title,
                path=prepared.output_path,
                version=prepared.version,
                assets=len(prepared.asset_records),
                warnings=prepared.warnings,
                losses=prepared.losses,
                push_safe=prepared.push_safe,
                status=prepared.status,
                blockers=prepared.blockers,
                migration_report=prepared.migration_report,
                migration_report_sha256=prepared.migration_report_sha256,
            )
            for prepared in prepared_pages
        ]

    batch = AttachmentWriteBatch()
    pending: list[_PendingPage] = []
    try:
        root_capability = batch.bind_directory(output_root)
        output_root = root_capability.directory
        for page_id in page_ids:
            page = client.get_page(page_id)
            page_capability = batch.bind_child_directory(
                root_capability,
                safe_page_directory_name(page.title, page_id),
            )
            page_directory = page_capability.directory
            asset_capability = batch.bind_child_directory(page_capability, "assets")
            markdown_name = f"{safe_attachment_filename(page.title, f'page_{page_id}')[:120]}.md"
            markdown_path = page_directory / markdown_name
            conversion = _convert_storage(page.body_storage or "", passthrough_prefixes)
            markdown = conversion.markdown or ""
            markdown, asset_count = _stage_sidecar_assets(
                client,
                page_id,
                markdown,
                asset_capability.directory,
                markdown_path,
                batch,
            )
            batch.add(markdown_path, markdown.encode("utf-8"))
            pending.append(
                _PendingPage(
                    page_id=page_id,
                    title=page.title,
                    path=markdown_path,
                    markdown=markdown,
                    version=_page_version(page),
                    assets=asset_count,
                    warnings=conversion.warnings,
                    losses=conversion.losses,
                    push_safe=conversion.push_safe,
                )
            )
        batch.commit()
    except BaseException:
        batch.abort()
        raise

    results: list[PullPageResult] = []
    for page in pending:
        results.append(
            PullPageResult(
                page_id=page.page_id,
                title=page.title,
                path=page.path,
                version=page.version,
                assets=page.assets,
                warnings=page.warnings,
                losses=page.losses,
                push_safe=page.push_safe,
            )
        )
    return results
