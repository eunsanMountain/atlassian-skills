"""RFE-001 R5: diff-local -- compare local md vs server canonical."""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any


def diff_local(
    client: Any,  # ConfluenceClient
    page_id: str,
    local_md_path: Path,
    *,
    passthrough_prefixes: list[str] | None = None,
) -> tuple[int, str]:
    """Compare local markdown vs server canonical.

    Returns:
        (exit_code, diff_output)
        exit_code 0 = identical, 1 = different (Unix diff convention)
    """
    # Read local md and GET server page
    local_md = local_md_path.read_text(encoding="utf-8")
    page = client.get_page(page_id)
    storage_body = page.body_storage or ""

    if passthrough_prefixes:
        import cfxmark

        opts = cfxmark.ConversionOptions(passthrough_html_comment_prefixes=tuple(passthrough_prefixes))
        local_storage = cfxmark.to_cfx(local_md, options=opts).xhtml or ""
        local_canonical = cfxmark.to_md(local_storage, options=opts).markdown or ""
        server_md = cfxmark.to_md(storage_body, options=opts).markdown or ""
    else:
        from atlassian_skills.core.format.markdown import confluence_storage_to_md, md_to_confluence_storage

        local_canonical = confluence_storage_to_md(md_to_confluence_storage(local_md))
        server_md = confluence_storage_to_md(storage_body)

    if local_canonical.strip() == server_md.strip():
        return 0, ""

    diff = difflib.unified_diff(
        server_md.splitlines(keepends=True),
        local_canonical.splitlines(keepends=True),
        fromfile="server",
        tofile="local",
    )
    return 1, "".join(diff)
