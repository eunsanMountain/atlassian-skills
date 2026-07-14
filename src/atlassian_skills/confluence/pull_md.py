"""Pull Confluence pages to Markdown with optional batched sidecar assets."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, NamedTuple

from atlassian_skills.core.attachment_io import (
    AttachmentWriteBatch,
    allocate_attachment_filename,
    atomic_write_bytes,
    safe_attachment_filename,
)
from atlassian_skills.core.errors import ValidationError


class PullResult(NamedTuple):
    """Result from pull_md containing markdown content and page metadata."""

    markdown: str
    version: int
    title: str


class PullPageResult(NamedTuple):
    """One persisted page returned by a multi-page pull."""

    page_id: str
    title: str
    path: Path
    version: int
    assets: int


class _PendingPage(NamedTuple):
    page_id: str
    title: str
    path: Path
    markdown: str
    version: int
    assets: int


_ASSET_MARKER_RE = re.compile(r'(!\[[^\]]*\]\()([^)]+)(\)<!-- cfxmark:asset src="([^"]*)" -->)')


def _page_version(page: Any) -> int:
    if page.version is None:
        return 1
    if hasattr(page.version, "number"):
        return int(page.version.number)
    if isinstance(page.version, int):
        return page.version
    return 1


def _convert_storage(storage_body: str, passthrough_prefixes: list[str] | None) -> str:
    if passthrough_prefixes:
        import cfxmark

        options = cfxmark.ConversionOptions(passthrough_html_comment_prefixes=tuple(passthrough_prefixes))
        result = cfxmark.to_md(storage_body, options=options)
        return result.markdown or ""

    from atlassian_skills.core.format.markdown import confluence_storage_to_md

    return confluence_storage_to_md(storage_body)


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

    attachments = client.list_attachments(page_id)
    attachment_map = {attachment.title: attachment for attachment in attachments}
    used_names: set[str] = set()
    replacements: dict[str, str] = {}

    for marker in markers:
        original_filename = marker.group(4)
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

    link_base = _sidecar_link_base(asset_dir, md_path)

    def replace_marker(match: re.Match[str]) -> str:
        stored_name = replacements.get(match.group(4))
        if stored_name is None:
            return match.group(0)
        return f"{match.group(1)}{link_base}{stored_name}{match.group(3)}"

    return _ASSET_MARKER_RE.sub(replace_marker, md_content), len(replacements)


def pull_md(
    client: Any,
    page_id: str,
    output_path: Path | None = None,
    *,
    passthrough_prefixes: list[str] | None = None,
    resolve_assets: str | None = None,
    asset_dir: Path | None = None,
) -> PullResult:
    """Pull one Confluence page and publish Markdown only after its assets succeed."""
    if resolve_assets is not None and resolve_assets != "sidecar":
        raise ValueError(f"Unknown resolve_assets mode: {resolve_assets!r} (expected 'sidecar')")

    page = client.get_page(page_id)
    md_content = _convert_storage(page.body_storage or "", passthrough_prefixes)
    batch = AttachmentWriteBatch()
    try:
        if resolve_assets == "sidecar" and asset_dir is not None:
            md_content, _ = _stage_sidecar_assets(client, page_id, md_content, asset_dir, output_path, batch)
        batch.commit()
    except BaseException:
        batch.abort()
        raise

    if output_path is not None:
        atomic_write_bytes(output_path, md_content.encode("utf-8"))
    return PullResult(markdown=md_content, version=_page_version(page), title=page.title)


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
) -> list[PullPageResult]:
    """Pull several pages with one client and one cross-page attachment commit."""
    if not page_ids:
        raise ValidationError("At least one Confluence page ID is required")
    if len(set(page_ids)) != len(page_ids):
        raise ValidationError("Duplicate Confluence page IDs are not allowed in one pull batch")

    output_root.mkdir(parents=True, exist_ok=True)
    batch = AttachmentWriteBatch()
    pending: list[_PendingPage] = []
    try:
        for page_id in page_ids:
            page = client.get_page(page_id)
            page_directory = output_root / safe_page_directory_name(page.title, page_id)
            markdown_name = f"{safe_attachment_filename(page.title, f'page_{page_id}')[:120]}.md"
            markdown_path = page_directory / markdown_name
            markdown = _convert_storage(page.body_storage or "", passthrough_prefixes)
            markdown, asset_count = _stage_sidecar_assets(
                client,
                page_id,
                markdown,
                page_directory / "assets",
                markdown_path,
                batch,
            )
            pending.append(
                _PendingPage(
                    page_id=page_id,
                    title=page.title,
                    path=markdown_path,
                    markdown=markdown,
                    version=_page_version(page),
                    assets=asset_count,
                )
            )
        batch.commit()
    except BaseException:
        batch.abort()
        raise

    results: list[PullPageResult] = []
    for page in pending:
        atomic_write_bytes(page.path, page.markdown.encode("utf-8"))
        results.append(
            PullPageResult(
                page_id=page.page_id,
                title=page.title,
                path=page.path,
                version=page.version,
                assets=page.assets,
            )
        )
    return results
