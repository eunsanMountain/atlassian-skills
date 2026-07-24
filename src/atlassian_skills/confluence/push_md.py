"""RFE-001 R2: push-md -- md -> canonicalize -> PUT + attachment upload."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from atlassian_skills.core.errors import (
    MigrationConsentRequiredError,
    ValidationError,
    consent_retry_action,
)

_READABLE_PROFILE_MARKER_RE = re.compile(
    r"<!--\s*atls:(?:mode=readable\s+push=blocked|profile=readable\s+push-safe=false)\s*-->"
)
_UNSUPPORTED_CONTENT_MARKER_RE = re.compile(r"<!--\s*cfxmark:unsupported\b[^>]*\bpush-safe=false\b[^>]*-->")
_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")


def _opening_fence(line: str) -> str | None:
    match = _FENCE_RE.match(line)
    if match is None:
        return None
    fence = match.group("fence")
    if fence[0] == "`" and "`" in match.group("info"):
        return None
    return fence


def _is_fence_close(line: str, fence_char: str, fence_length: int) -> bool:
    stripped = line.lstrip(" ")
    candidate = stripped.strip()
    return (
        len(line) - len(stripped) <= 3
        and len(candidate) >= fence_length
        and bool(candidate)
        and set(candidate) == {fence_char}
    )


def _strip_inline_code(line: str) -> str:
    """Hide inline code spans before scanning control comments."""

    chars = list(line)
    cursor = 0
    while cursor < len(line):
        if line[cursor] != "`":
            cursor += 1
            continue
        end = cursor
        while end < len(line) and line[end] == "`":
            end += 1
        fence = line[cursor:end]
        close = line.find(fence, end)
        if close < 0:
            cursor = end
            continue
        chars[cursor : close + len(fence)] = " " * (close + len(fence) - cursor)
        cursor = close + len(fence)
    return "".join(chars)


def _push_control_text(md_content: str) -> str:
    """Return only prose and comments that can control publication safety."""

    visible: list[str] = []
    fence_char = ""
    fence_length = 0
    for line in md_content.splitlines():
        if fence_char:
            if _is_fence_close(line, fence_char, fence_length):
                fence_char = ""
                fence_length = 0
            continue
        fence = _opening_fence(line)
        if fence is not None:
            fence_char = fence[0]
            fence_length = len(fence)
            continue
        if line.startswith(("    ", "\t")):
            continue
        visible.append(_strip_inline_code(line))
    return "\n".join(visible)


def _assert_push_safe_source(md_content: str) -> None:
    """Reject Markdown that explicitly declares itself unsafe to publish."""

    from atlassian_skills.core.format.markdown import ReadableMarkdown

    if isinstance(md_content, ReadableMarkdown):
        raise ValidationError(
            "This Markdown was generated for reading only. Use confluence page pull-md before editing and publishing."
        )
    control_text = _push_control_text(md_content)
    if _READABLE_PROFILE_MARKER_RE.search(control_text):
        raise ValidationError(
            "This Markdown was generated for reading only. Use confluence page pull-md before editing and publishing."
        )
    if _UNSUPPORTED_CONTENT_MARKER_RE.search(control_text):
        raise ValidationError("This Markdown contains unsupported Confluence content and cannot be published safely.")


def _managed_retry_argv(
    page_id: str,
    managed_path: Path,
    *,
    passthrough_prefixes: list[str] | None,
    if_version: int | None,
    reason: str | None,
    minor_edit: bool,
) -> tuple[str, ...]:
    argv = ["atls", "confluence", "page", "push-md", page_id, "--md-file", str(managed_path)]
    for prefix in passthrough_prefixes or ():
        argv.extend(("--passthrough-prefix", prefix))
    if if_version is not None:
        argv.extend(("--if-version", str(if_version)))
    if reason is not None:
        argv.extend(("--reason", reason))
    if minor_edit:
        argv.append("--minor-edit")
    return tuple(argv)


def push_md(
    client: Any,  # ConfluenceClient
    page_id: str,
    md_content: str,
    *,
    passthrough_prefixes: list[str] | None = None,
    dry_run: bool = False,
    attachments: list[Path] | None = None,
    attachment_if_exists: str | None = None,
    if_version: int | None = None,
    managed_path: Path | None = None,
    reason: str | None = None,
    minor_edit: bool = False,
    accept_migration: str | None = None,
    next_action_argv: tuple[str, ...] | None = None,
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
    if managed_path is not None:
        if attachments or attachment_if_exists is not None:
            raise ValidationError(
                "Managed Markdown derives its asset plan from the portable manifest.",
                context={"reason": "managed_asset_flags_removed"},
            )
        from atlassian_skills.confluence.body_write import recover_managed_body

        retry_argv = next_action_argv or _managed_retry_argv(
            page_id,
            managed_path,
            passthrough_prefixes=passthrough_prefixes,
            if_version=if_version,
            reason=reason,
            minor_edit=minor_edit,
        )
        recovery = recover_managed_body(
            client,
            page_id,
            managed_path,
            passthrough_prefixes=tuple(passthrough_prefixes) if passthrough_prefixes is not None else None,
            if_version=if_version,
            dry_run=dry_run,
            accept_migration=accept_migration,
            next_action_argv=retry_argv,
            reason=reason,
            minor_edit=minor_edit,
        )
        if recovery is not None:
            return recovery
        from atlassian_skills.confluence.migration_preflight import build_managed_preflight

        managed_preflight = build_managed_preflight(
            client,
            page_id,
            managed_path,
            passthrough_prefixes=tuple(passthrough_prefixes) if passthrough_prefixes else None,
        )
        if if_version is not None and managed_preflight.remote_version != if_version:
            from atlassian_skills.core.errors import StaleError

            raise StaleError(
                f"Version mismatch: expected {if_version}, server has {managed_preflight.remote_version}",
                context={"server_version": managed_preflight.remote_version, "expected_version": if_version},
            )
        result = managed_preflight.to_dict()
        consent_action: dict[str, Any] | None = None
        if managed_preflight.consent_required:
            if managed_preflight.migration_fingerprint is None:
                raise ValidationError(
                    "Consent-required preflight has no migration fingerprint",
                    context={"reason": "migration_fingerprint_missing"},
                )
            consent_action = consent_retry_action(
                retry_argv,
                option="--accept-migration",
                fingerprint=managed_preflight.migration_fingerprint,
                description_code="REVIEW_MIGRATION_AND_RETRY",
            )
        if dry_run:
            return {
                **result,
                "dry_run": True,
                **({"next_actions": [consent_action]} if consent_action is not None else {}),
            }
        if managed_preflight.status == "no_change":
            # Uniform with the stateless no_change receipt: a proven no-op performed no
            # PUT, so an external receipt consumer never has to special-case managed.
            return {**result, "put_count": 0}
        if managed_preflight.consent_required and accept_migration != managed_preflight.migration_fingerprint:
            assert consent_action is not None
            from atlassian_skills.confluence.migration_preflight import to_error_context

            raise MigrationConsentRequiredError(
                "Markdown migration requires explicit informed consent",
                hint="Review the loss summary before running the returned command.",
                context={
                    **to_error_context(result),
                    "reason": "migration_consent_required",
                    "accepted": False,
                    "next_actions": [consent_action],
                },
            )
        from atlassian_skills.confluence.body_write import publish_managed_body

        return publish_managed_body(
            client,
            managed_preflight,
            managed_path,
            accept_migration=accept_migration,
            next_action_argv=retry_argv,
            reason=reason,
            minor_edit=minor_edit,
        )
    _assert_push_safe_source(md_content)

    from atlassian_skills.core.format.markdown import md_to_confluence_storage_result

    local_conversion = md_to_confluence_storage_result(
        md_content,
        passthrough_prefixes=tuple(passthrough_prefixes or ()),
    )
    if not local_conversion.push_safe or local_conversion.losses:
        raise ValidationError(
            "The local Markdown cannot be converted without losing content.",
            hint="Resolve the reported conversion losses before publishing.",
            context={
                "push_safe": local_conversion.push_safe,
                "warnings": list(local_conversion.warnings),
                "losses": list(local_conversion.losses),
            },
        )
    storage_body = local_conversion.body

    # GET current page
    page = client.get_page(page_id)
    current_body = page.body_storage or ""

    from atlassian_skills.core.format.markdown import confluence_storage_to_md_result

    current_conversion = confluence_storage_to_md_result(
        current_body,
        profile="editable",
        passthrough_prefixes=tuple(passthrough_prefixes or ()),
    )
    if not current_conversion.push_safe:
        raise ValidationError(
            "The current Confluence page contains unsupported content and cannot be published safely.",
            hint="Preserve the page and inspect it with confluence page pull-md before editing.",
            context={
                "push_safe": False,
                "warnings": list(current_conversion.warnings),
                "losses": list(current_conversion.losses),
            },
        )

    conversion_warnings = local_conversion.warnings + tuple(
        f"current page: {warning}" for warning in current_conversion.warnings
    )

    def result_with_diagnostics(result: dict[str, Any]) -> dict[str, Any]:
        if conversion_warnings:
            result["conversion"] = {
                "push_safe": True,
                "warnings": list(conversion_warnings),
                "losses": [],
            }
        return result

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

    if _storage_equivalent(
        storage_body,
        current_body,
        passthrough_prefixes=tuple(passthrough_prefixes or ()),
    ):
        return result_with_diagnostics(
            {"status": "no_change", "page_id": page_id, "version": current_version, "put_count": 0}
        )

    if dry_run:
        return result_with_diagnostics(
            {
                "status": "dry_run",
                "page_id": page_id,
                "dry_run": True,
                "would_update": True,
                "version": current_version + 1,
            }
        )

    # PUT with new version
    new_version = current_version + 1
    version_options: dict[str, Any] = {}
    if reason is not None:
        version_options["reason"] = reason
    if minor_edit:
        version_options["minor_edit"] = True
    client.update_page(
        page_id=page_id,
        title=title,
        body=storage_body,
        version_number=new_version,
        **version_options,
    )

    # Upload attachments if any (FR-6: use batch with if_exists)
    if attachments:
        client.upload_attachments_batch(
            page_id,
            [str(p) for p in attachments],
            if_exists=attachment_if_exists or "replace",
        )

    return result_with_diagnostics({"status": "updated", "page_id": page_id, "version": new_version})


def _storage_equivalent(
    local_body: str,
    server_body: str,
    *,
    passthrough_prefixes: tuple[str, ...],
) -> bool:
    """Compare safe storage through editable Markdown after server normalization."""
    if local_body == server_body:
        return True

    from atlassian_skills.core.format.markdown import confluence_storage_to_md_result

    local = confluence_storage_to_md_result(
        local_body,
        profile="editable",
        passthrough_prefixes=passthrough_prefixes,
    )
    server = confluence_storage_to_md_result(
        server_body,
        profile="editable",
        passthrough_prefixes=passthrough_prefixes,
    )
    return bool(local.push_safe and server.push_safe and local.markdown == server.markdown)
