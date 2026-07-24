"""Compare local Markdown with the server's editable canonical form."""

from __future__ import annotations

import difflib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cfxmark

from atlassian_skills.confluence.push_md import _assert_push_safe_source
from atlassian_skills.core.errors import ValidationError
from atlassian_skills.core.managed_file import read_managed_utf8


@dataclass(frozen=True)
class DiffResult:
    """Diff output plus conversion safety diagnostics."""

    exit_code: int
    diff_output: str
    push_safe: bool
    warnings: tuple[str, ...] = ()
    losses: tuple[str, ...] = ()

    def __iter__(self) -> Iterator[Any]:
        """Keep the historical ``exit_code, diff_output = result`` API."""

        yield self.exit_code
        yield self.diff_output


def _prefixed(prefix: str, messages: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"{prefix}: {message}" for message in messages)


def diff_local(
    client: Any,  # ConfluenceClient
    page_id: str,
    local_md_path: Path,
    *,
    passthrough_prefixes: list[str] | None = None,
) -> DiffResult:
    """Compare local Markdown with server content using editable conversion."""

    local_md = read_managed_utf8(local_md_path, reason="local_markdown_read_failed")
    page = client.get_page(page_id)
    storage_body = page.body_storage or ""
    options = cfxmark.ConversionOptions(
        profile="editable",
        passthrough_html_comment_prefixes=tuple(passthrough_prefixes or ()),
    )

    local_warnings: tuple[str, ...] = ()
    local_losses: tuple[str, ...] = ()
    local_push_safe = True
    try:
        _assert_push_safe_source(local_md)
        local_storage = cfxmark.to_cfx(local_md, options=options).xhtml or ""
        local_result = cfxmark.to_md(local_storage, options=options)
        local_canonical = local_result.markdown or ""
        local_push_safe = local_result.push_safe
        local_warnings = local_result.warnings
        local_losses = local_result.losses
        if not local_push_safe and not local_losses:
            local_losses = ("conversion is not push-safe",)
    except (ValidationError, cfxmark.ConversionError) as error:
        local_canonical = local_md
        local_push_safe = False
        local_losses = (str(error),)

    server_result = cfxmark.to_md(storage_body, options=options)
    server_md = server_result.markdown or ""
    server_losses = server_result.losses
    if not server_result.push_safe and not server_losses:
        server_losses = ("conversion is not push-safe",)
    warnings = _prefixed("local", local_warnings) + _prefixed("server", server_result.warnings)
    losses = _prefixed("local", local_losses) + _prefixed("server", server_losses)
    push_safe = bool(local_push_safe and server_result.push_safe)

    if local_canonical == server_md:
        return DiffResult(0, "", push_safe, warnings, losses)

    diff = difflib.unified_diff(
        server_md.splitlines(keepends=True),
        local_canonical.splitlines(keepends=True),
        fromfile="server",
        tofile="local",
    )
    return DiffResult(1, "".join(diff), push_safe, warnings, losses)
