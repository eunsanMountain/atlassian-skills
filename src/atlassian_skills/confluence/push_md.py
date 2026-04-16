"""RFE-001 R2: push-md -- md -> canonicalize -> PUT + attachment upload."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def push_md(
    client: Any,  # ConfluenceClient
    page_id: str,
    md_content: str,
    *,
    passthrough_prefixes: list[str] | None = None,
    dry_run: bool = False,
    attachments: list[Path] | None = None,
    attachment_if_exists: str = "replace",
    if_version: int | None = None,
) -> dict[str, Any]:
    """Push local markdown to Confluence page.

    1. Convert md to Confluence storage format via cfxmark
    2. GET current page (version + body)
    3. Optionally check --if-version (stale guard)
    4. Canonicalize both local and server body
    5. Compare: if identical, return no_change dict
    6. If different and not dry_run, PUT with version+1
    7. Upload any attachments

    Returns:
        Always a dict with at least {status, page_id, version}.
    """
    # Convert md -> storage
    if passthrough_prefixes:
        import cfxmark

        opts = cfxmark.ConversionOptions(passthrough_html_comment_prefixes=tuple(passthrough_prefixes))
        result = cfxmark.to_cfx(md_content, options=opts)
        storage_body = result.xhtml or ""
    else:
        from atlassian_skills.core.format.markdown import md_to_confluence_storage

        storage_body = md_to_confluence_storage(md_content)

    # GET current page
    page = client.get_page(page_id)
    current_body = page.body_storage or ""

    current_version = 1
    if page.version is not None:
        if hasattr(page.version, "number"):
            current_version = page.version.number
        elif isinstance(page.version, int):
            current_version = page.version

    title = page.title

    # Stale check (FR-1)
    if if_version is not None and current_version != if_version:
        from atlassian_skills.core.errors import StaleError

        raise StaleError(
            f"Version mismatch: expected {if_version}, server has {current_version}",
            context={"server_version": current_version, "expected_version": if_version},
        )

    # Compare (simple string compare after normalize whitespace)
    if _normalize(storage_body) == _normalize(current_body):
        return {"status": "no_change", "page_id": page_id, "version": current_version}

    if dry_run:
        return {
            "status": "dry_run",
            "page_id": page_id,
            "dry_run": True,
            "would_update": True,
            "version": current_version + 1,
        }

    # PUT with new version
    new_version = current_version + 1
    client.update_page(
        page_id=page_id,
        title=title,
        body=storage_body,
        version_number=new_version,
    )

    # Upload attachments if any (FR-6: use batch with if_exists)
    if attachments:
        client.upload_attachments_batch(
            page_id,
            [str(p) for p in attachments],
            if_exists=attachment_if_exists,
        )

    return {"status": "updated", "page_id": page_id, "version": new_version}


def _normalize(text: str) -> str:
    """Normalize whitespace for comparison."""
    return " ".join(text.split())
