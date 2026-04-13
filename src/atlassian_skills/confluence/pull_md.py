"""RFE-001 R3: pull-md -- GET -> md -> asset resolve -> file write."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, NamedTuple


class PullResult(NamedTuple):
    """Result from pull_md containing markdown content and page metadata."""

    markdown: str
    version: int
    title: str


# Regex to match cfxmark asset markers: ![alt](src)<!-- cfxmark:asset src="filename" -->
_ASSET_MARKER_RE = re.compile(
    r'(!\[[^\]]*\]\()([^)]+)(\)<!-- cfxmark:asset src="([^"]*)" -->)'
)


def pull_md(
    client: Any,  # ConfluenceClient
    page_id: str,
    output_path: Path | None = None,
    *,
    passthrough_prefixes: list[str] | None = None,
    resolve_assets: str | None = None,
    asset_dir: Path | None = None,
) -> PullResult:
    """Pull Confluence page as markdown.

    1. GET page with body.storage
    2. Convert storage -> md via cfxmark
    3. Optionally resolve assets (sidecar mode)
    4. If output_path, write to file
    5. Return PullResult with markdown, version, title
    """
    page = client.get_page(page_id)
    storage_body = page.body_storage or ""

    # Extract version
    version = 1
    if page.version is not None:
        if hasattr(page.version, "number"):
            version = page.version.number
        elif isinstance(page.version, int):
            version = page.version
    title = page.title

    if passthrough_prefixes:
        import cfxmark

        opts = cfxmark.ConversionOptions(passthrough_html_comment_prefixes=tuple(passthrough_prefixes))
        result = cfxmark.to_md(storage_body, options=opts)
        md_content = result.markdown or ""
    else:
        from atlassian_skills.core.format.markdown import confluence_storage_to_md

        md_content = confluence_storage_to_md(storage_body)

    # FR-5: resolve assets in sidecar mode
    if resolve_assets is not None and resolve_assets != "sidecar":
        msg = f"Unknown resolve_assets mode: {resolve_assets!r} (expected 'sidecar')"
        raise ValueError(msg)
    if resolve_assets == "sidecar" and asset_dir is not None:
        md_content = _resolve_assets_sidecar(client, page_id, md_content, asset_dir, output_path)

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(md_content, encoding="utf-8")

    return PullResult(markdown=md_content, version=version, title=title)


def _resolve_assets_sidecar(
    client: Any,
    page_id: str,
    md_content: str,
    asset_dir: Path,
    md_path: Path | None = None,
) -> str:
    """Download attachments referenced by cfxmark:asset markers and rewrite links.

    Parses ``<!-- cfxmark:asset src="filename" -->`` markers from cfxmark output,
    downloads matching attachments to asset_dir, and rewrites image links to
    relative paths.  Markers are preserved for round-trip safety.
    """
    markers = _ASSET_MARKER_RE.findall(md_content)
    if not markers:
        return md_content

    # Build filename -> (attachment_id, download_link) map
    attachments = client.list_attachments(page_id)
    att_map: dict[str, tuple[str, str | None]] = {
        a.title: (a.id, a.links.download if a.links else None) for a in attachments
    }

    # Compute relative link base
    if md_path is not None:
        try:
            link_base = str(asset_dir.relative_to(md_path.parent)) + "/"
        except ValueError:
            link_base = asset_dir.name + "/"
    else:
        link_base = asset_dir.name + "/"

    asset_dir.mkdir(parents=True, exist_ok=True)

    for _prefix, _old_src, _suffix, original_filename in markers:
        if original_filename not in att_map:
            continue
        att_id, dl_link = att_map[original_filename]
        dest = asset_dir / original_filename
        client.download_attachment(att_id, dest, download_link=dl_link)

        # Rewrite: ![alt](old_src)<!-- ... --> -> ![alt](assets/filename)<!-- ... -->
        new_src = f"{link_base}{original_filename}"
        old_pattern = f"{_prefix}{_old_src}{_suffix}"
        new_pattern = f"{_prefix}{new_src}{_suffix}"
        md_content = md_content.replace(old_pattern, new_pattern, 1)

    return md_content
