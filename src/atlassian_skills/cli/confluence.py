from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import typer
from cfxmark.presentation import extract_presentation

from atlassian_skills.confluence.client import ConfluenceClient
from atlassian_skills.confluence.migration_preflight import describe_migration_code
from atlassian_skills.confluence.page_copy import copy_page as copy_confluence_page
from atlassian_skills.core.auth import resolve_credential
from atlassian_skills.core.config import get_profile, load_config
from atlassian_skills.core.dryrun import format_dry_run
from atlassian_skills.core.errors import AtlasError, ExitCode, NotFoundError, ValidationError, consent_retry_action
from atlassian_skills.core.format import OutputFormat, format_output
from atlassian_skills.core.format.markdown import (
    WriteConversionResult,
    confluence_storage_to_md_result,
    md_to_confluence_storage_result,
)
from atlassian_skills.core.models import WriteResult
from atlassian_skills.core.stdin import read_body

confluence_app = typer.Typer(help="Confluence commands", no_args_is_help=True)

# Sub-groups
page_app = typer.Typer(
    help="Page commands (passthrough prefixes are supported by push-md, pull-md, and diff-local)",
    no_args_is_help=True,
)
space_app = typer.Typer(help="Space commands", no_args_is_help=True)
comment_app = typer.Typer(help="Comment commands", no_args_is_help=True)
label_app = typer.Typer(help="Label commands", no_args_is_help=True)
attachment_app = typer.Typer(help="Attachment commands", no_args_is_help=True)
user_app = typer.Typer(help="User commands", no_args_is_help=True)

confluence_app.add_typer(page_app, name="page")
confluence_app.add_typer(space_app, name="space")
confluence_app.add_typer(comment_app, name="comment")
confluence_app.add_typer(label_app, name="label")
confluence_app.add_typer(attachment_app, name="attachment")
confluence_app.add_typer(user_app, name="user")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(ctx_obj: dict[str, Any]) -> ConfluenceClient:
    profile_name: str = ctx_obj.get("profile", "default")
    timeout: float = ctx_obj.get("timeout", 30.0)
    config = load_config()
    profile = get_profile(config, profile_name)
    url = profile.confluence_url or os.environ.get(f"ATLS_{profile_name.upper()}_CONFLUENCE_URL")
    if not url:
        typer.echo(
            f"No Confluence URL for profile '{profile_name}'. "
            f"Set confluence_url in config or ATLS_{profile_name.upper()}_CONFLUENCE_URL env var.",
            err=True,
        )
        raise typer.Exit(1)
    credential = resolve_credential(profile_name, "confluence", profile)
    verify: str | bool = profile.ca_bundle if profile.ca_bundle else True
    return ConfluenceClient(url.rstrip("/"), credential, timeout=timeout, verify=verify)


def _fmt(ctx_obj: dict[str, Any]) -> OutputFormat:
    fmt = ctx_obj.get("format", OutputFormat.COMPACT)
    return OutputFormat(fmt) if not isinstance(fmt, OutputFormat) else fmt


def _resolve_fmt(ctx_obj: dict[str, Any], local_format: str | None) -> OutputFormat:
    if local_format:
        try:
            return OutputFormat(local_format)
        except ValueError:
            valid = ", ".join(f.value for f in OutputFormat)
            typer.echo(f"Error: Invalid format '{local_format}'. Valid: {valid}", err=True)
            raise typer.Exit(1)  # noqa: B904
    return _fmt(ctx_obj)


def _conversion_diagnostics(
    warnings: tuple[str, ...],
    losses: tuple[str, ...],
    push_safe: bool,
    *,
    table_background_omitted_count: int = 0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "push_safe": push_safe,
        "warnings": list(warnings),
        "losses": list(losses),
    }
    if table_background_omitted_count:
        payload["diagnostics"] = [
            {
                "code": "table-cell-background-omitted",
                "severity": "warning",
                "count": table_background_omitted_count,
                "message": (
                    f"Readable Markdown omits the backgrounds of {table_background_omitted_count} table cells; "
                    "the remote page remains the presentation source of truth."
                ),
            }
        ]
    return payload


def _emit_conversion_diagnostics(
    ctx: typer.Context,
    warnings: tuple[str, ...],
    losses: tuple[str, ...],
    push_safe: bool,
    *,
    table_background_omitted_count: int = 0,
) -> None:
    if ctx.obj.get("quiet"):
        return
    if not push_safe:
        typer.echo("# conversion: push_safe=false", err=True)
    for message in (*warnings, *losses):
        typer.echo(f"# conversion: {message}", err=True)
    if table_background_omitted_count:
        typer.echo(
            "# conversion: table-cell-background-omitted "
            f"count={table_background_omitted_count}; readable Markdown does not display these backgrounds",
            err=True,
        )


def _readable_table_background_omission_count(conversion: Any) -> int:
    if conversion.document is None:
        return 0
    return sum(cell.background is not None for cell in extract_presentation(conversion.document).cells)


def _output_with_write_diagnostics(
    ctx: typer.Context,
    output: Any,
    fmt: OutputFormat,
    conversion: WriteConversionResult,
) -> Any:
    has_diagnostics = bool(conversion.warnings or conversion.losses or not conversion.push_safe)
    if not has_diagnostics:
        return output
    if fmt != OutputFormat.JSON:
        _emit_conversion_diagnostics(
            ctx,
            conversion.warnings,
            conversion.losses,
            conversion.push_safe,
        )
        return output
    if isinstance(output, dict):
        payload: Any = dict(output)
    elif hasattr(output, "model_dump"):
        payload = output.model_dump(mode="json", exclude_none=True)
    else:
        payload = output
    if isinstance(payload, dict):
        payload["conversion"] = _conversion_diagnostics(
            conversion.warnings,
            conversion.losses,
            conversion.push_safe,
        )
        return payload
    return {
        "result": payload,
        "conversion": _conversion_diagnostics(
            conversion.warnings,
            conversion.losses,
            conversion.push_safe,
        ),
    }


# Counts that explain *why* a selection failed. JSON callers already receive the
# whole context; without these lines the human reader loses the one number that
# distinguishes "no match" from "three matches" and has to re-run with
# --format=json to find out.
# (context key, label, always show). The primary count is always shown because
# "0 matches" is itself the answer; the other two are noise when zero.
_DIAGNOSTIC_COUNT_KEYS = (
    ("match_count", "matches", True),
    ("boundary_match_count", "spanning inline markup", False),
    ("excluded_match_count", "in attributes or macros", False),
)

# Allowlisted local templates. A description_code that is not listed here is
# ignored rather than echoed, so no server-provided string can reach the user
# as if it were an instruction from atls.
_NEXT_ACTION_TEXT = {
    "PATCH_RETRY_SINGLE_LEAF": "Retry --find with the exact text of a single plain-text part.",
    "PATCH_USE_MANAGED_EDIT": "For structural or macro content, use pull-md and edit as Markdown.",
}

_CONSENT_ACTIONS = {
    "REVIEW_MIGRATION_AND_RETRY": (
        "--accept-migration",
        "mig_sha256:",
        "Use page inspect and patch-text for a narrow plain-text edit, or revise the managed Markdown and rerun "
        "--dry-run.",
    ),
    "REVIEW_CONVERSION_AND_RETRY": (
        "--accept-conversion",
        "conv_sha256:",
        "Revise unsupported Markdown constructs and rerun --dry-run before approving conversion.",
    ),
}

_MIGRATION_EFFECT_LABELS = {
    "converted": "converted",
    "normalized": "normalized",
    "removed": "removed",
    "unsupported": "unsupported",
    "fatal": "fatal",
}

_DIAGNOSTIC_CATEGORY_LABELS = {
    "readable_simplification": "readable simplification",
    "canonicalization": "canonicalization",
    "presentation_loss": "presentation loss",
    "content_loss": "content loss",
    "structural_loss": "structural loss",
    "manual_replacement": "manual replacement",
}


def _report_groups(report: Any, field: str, labels: dict[str, str]) -> list[str]:
    if not isinstance(report, dict) or not isinstance(report.get("occurrences"), list):
        return []
    counts = Counter(
        occurrence.get(field)
        for occurrence in report["occurrences"]
        if isinstance(occurrence, dict) and occurrence.get(field) in labels
    )
    return [f"{label}={counts[value]}" for value, label in labels.items() if counts[value]]


def _consent_loss_summary(context: dict[str, Any]) -> str | None:
    groups = _report_groups(context.get("migration_report"), "effect", _MIGRATION_EFFECT_LABELS)
    groups.extend(_report_groups(context.get("source_conversion_report"), "category", _DIAGNOSTIC_CATEGORY_LABELS))
    if not groups:
        conversion = context.get("conversion")
        losses = conversion.get("losses") if isinstance(conversion, dict) else None
        if isinstance(losses, list) and losses:
            groups.append(f"source loss={len(losses)}")
    return ", ".join(groups) if groups else None


def _safe_loss_text(value: Any, *, limit: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    flattened = " ".join("".join(character if character.isprintable() else " " for character in value).split())
    if not flattened:
        return None
    return flattened if len(flattened) <= limit else flattened[: limit - 1] + "…"


def _consent_loss_details(context: dict[str, Any], *, limit: int = 5) -> tuple[str, ...]:
    details: list[str] = []
    total = 0
    for report_key in ("migration_report", "source_conversion_report"):
        report = context.get(report_key)
        occurrences = report.get("occurrences") if isinstance(report, dict) else None
        if not isinstance(occurrences, list):
            continue
        for occurrence in occurrences:
            if not isinstance(occurrence, dict):
                continue
            total += 1
            if len(details) >= limit:
                continue
            # Prefer the atls-owned value-free curated description for a known stable
            # code (shown with the code for traceability); the value-free JSON envelope
            # already strips cfxmark's display_label, so this restores console
            # readability without reintroducing any arbitrary-content leak. Fall back to
            # a display_label only when a caller passes an un-redacted context, then to
            # the raw stable code.
            code_value = occurrence.get("code")
            described = describe_migration_code(code_value if isinstance(code_value, str) else None)
            if described is not None:
                label: str | None = f"{described} ({code_value})"
            else:
                label = _safe_loss_text(occurrence.get("display_label")) or _safe_loss_text(code_value)
            impact = _safe_loss_text(occurrence.get("user_impact"))
            before = _safe_loss_text(occurrence.get("before_summary"))
            after = _safe_loss_text(occurrence.get("after_summary"))
            workflow = _safe_loss_text(occurrence.get("suggested_workflow"))
            lines = [f"Loss detail {len(details) + 1}: {label or 'unlabelled migration'}"]
            if impact:
                lines.append(f"  Impact: {impact}")
            if before or after:
                lines.append(f"  Change: {before or 'unknown'} -> {after or 'unknown'}")
            if workflow:
                lines.append(f"  Suggested: {workflow}")
            details.append("\n".join(lines))
    if total > limit:
        details.append(f"Loss detail: {total - limit} additional occurrence(s) omitted; use --format=json for all.")
    return tuple(details)


def _consent_retry_display(action: Any) -> tuple[str, str] | None:
    if not isinstance(action, dict) or action.get("id") != "retry_with_consent":
        return None
    if action.get("requires_user_approval") is not True:
        return None
    rule = _CONSENT_ACTIONS.get(str(action.get("description_code")))
    argv = action.get("argv")
    if rule is None or not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
        return None
    consent_option, fingerprint_prefix, alternative = rule
    if argv[:3] != ["atls", "confluence", "page"] or argv[-2:-1] != [consent_option]:
        return None
    digest = argv[-1].removeprefix(fingerprint_prefix)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return None
    display = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    return alternative, display


def _handle_error(err: AtlasError, fmt: OutputFormat) -> None:
    if fmt == OutputFormat.JSON:
        typer.echo(json.dumps(err.to_dict()))
    else:
        typer.echo(f"Error: {err.message}", err=True)
        context = err.context or {}
        counts = [
            f"{context[key]} {label}"
            for key, label, always in _DIAGNOSTIC_COUNT_KEYS
            if isinstance(context.get(key), int) and (always or context[key])
        ]
        if counts:
            typer.echo(f"Found: {', '.join(counts)}", err=True)
        actions = context.get("next_actions")
        consent_retries = (
            [retry for action in actions if (retry := _consent_retry_display(action)) is not None]
            if isinstance(actions, list)
            else []
        )
        if consent_retries:
            summary = _consent_loss_summary(context)
            if summary:
                typer.echo(f"Loss summary: {summary}", err=True)
                for detail in _consent_loss_details(context):
                    typer.echo(detail, err=True)
                for alternative, _command in consent_retries:
                    typer.echo(f"Alternative: {alternative}", err=True)
            else:
                typer.echo("Loss summary: unavailable; retry command withheld.", err=True)
                consent_retries = []
        if err.hint:
            typer.echo(f"Hint:  {err.hint}", err=True)
        if isinstance(actions, list):
            for action in actions:
                text = _NEXT_ACTION_TEXT.get(str(action.get("description_code")))
                if text:
                    typer.echo(f"Next:  {text}", err=True)
        for _alternative, command in consent_retries:
            typer.echo("", err=True)
            typer.echo(f"Retry: {command}", err=True)
    raise typer.Exit(err.exit_code)


# ---------------------------------------------------------------------------
# page get <id>
# ---------------------------------------------------------------------------


@page_app.command("get")
def page_get(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    body_repr: str | None = typer.Option(None, "--body-repr", help="Body representation: md|raw|storage|view"),
    passthrough_prefix: list[str] | None = typer.Option(
        None,
        "--passthrough-prefix",
        help="Preserve an additional HTML comment prefix during Markdown conversion (repeatable)",
    ),
    format: str | None = typer.Option(None, "--format", "-f", help="Override output format"),
) -> None:
    """Get a Confluence page by ID."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)

    try:
        if body_repr not in {None, "md", "raw", "storage", "view"}:
            raise ValidationError(
                "--body-repr must be md, raw, storage, or view",
                context={"reason": "invalid_body_representation", "body_repr": body_repr},
            )
        markdown_conversion = body_repr == "md" or (body_repr is None and fmt == OutputFormat.MD)
        if passthrough_prefix and not markdown_conversion:
            raise ValidationError(
                "--passthrough-prefix requires Markdown body conversion",
                context={"reason": "passthrough_requires_markdown_conversion"},
            )
        from atlassian_skills.core.managed_manifest import (
            ManagedManifestError,
            parse_passthrough,
            serialize_passthrough,
        )

        try:
            canonical_prefixes = parse_passthrough(serialize_passthrough(passthrough_prefix or ()))
        except ManagedManifestError as error:
            raise ValidationError("Invalid passthrough prefix", context=error.context) from error
        client = _make_client(ctx.obj)

        # RAW format: return server response text verbatim (byte-preserving contract)
        if fmt == OutputFormat.RAW and body_repr is None:
            typer.echo(client.get_page_raw_text(page_id))
            return

        if body_repr == "view":
            page = client.get_page(page_id, expand="body.view,version,space,history", include_body=True)
            if not isinstance(page.body_view, str):
                raise ValidationError(
                    "Confluence rendered view body is missing",
                    context={"reason": "view_body_missing", "page_id": page_id},
                )
            if fmt == OutputFormat.RAW:
                typer.echo(page.body_view, nl=False)
            elif fmt == OutputFormat.JSON:
                payload = page.model_dump(mode="json")
                payload.update(
                    {
                        "representation": "view",
                        "editable": False,
                        "publishable": False,
                        "reason": "server-rendered-html",
                    }
                )
                typer.echo(format_output(payload, fmt))
            else:
                typer.echo(page.body_view)
            return

        needs_body = body_repr in ("md", "raw", "storage") or fmt == OutputFormat.MD
        include_body = needs_body or fmt != OutputFormat.COMPACT
        page = client.get_page(page_id, include_body=include_body)

        conversion = None
        if page.body_storage is not None and markdown_conversion:
            conversion = confluence_storage_to_md_result(
                page.body_storage,
                profile="readable",
                passthrough_prefixes=canonical_prefixes,
            )
            if body_repr == "md":
                page.body_storage = "" if page.body_storage == "" else conversion.markdown or ""
        # "raw" and "storage" keep the storage XHTML as-is

        if fmt == OutputFormat.JSON and conversion is not None:
            table_background_omitted_count = _readable_table_background_omission_count(conversion)
            payload = page.model_dump(mode="json")
            payload["conversion"] = _conversion_diagnostics(
                conversion.warnings,
                conversion.losses,
                conversion.push_safe,
                table_background_omitted_count=table_background_omitted_count,
            )
            payload["representation"] = "md"
            payload["editable"] = False
            payload["publishable"] = False
            payload["conversion_options"] = {"passthrough_prefixes": list(canonical_prefixes)}
            typer.echo(format_output(payload, fmt))
        elif body_repr == "md" and conversion is not None:
            typer.echo(conversion.markdown or "", nl=False)
        elif fmt == OutputFormat.MD and conversion is not None:
            from atlassian_skills.core.format.markdown import format_page_md_document, format_page_md_header

            space_key = page.space.key if page.space else ""
            header = format_page_md_header(page.title, space_key, page.version)
            typer.echo(format_page_md_document(header, conversion.markdown or ""))
        elif fmt == OutputFormat.RAW and body_repr in {"raw", "storage"}:
            typer.echo(page.body_storage or "", nl=False)
        elif fmt == OutputFormat.MD and body_repr:
            from atlassian_skills.core.format.markdown import format_page_md_header

            space_key = page.space.key if page.space else ""
            header = format_page_md_header(page.title, space_key, page.version)
            typer.echo(header + (page.body_storage or ""))
        else:
            typer.echo(format_output(page, fmt))
        if conversion is not None and fmt != OutputFormat.JSON:
            _emit_conversion_diagnostics(
                ctx,
                conversion.warnings,
                conversion.losses,
                conversion.push_safe,
                table_background_omitted_count=_readable_table_background_omission_count(conversion),
            )
    except AtlasError as e:
        _handle_error(e, fmt)


@page_app.command("inspect")
def page_inspect(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    intent: str = typer.Option(..., "--intent", help="read|text-edit|append|structure-edit|presentation-edit"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Inspect a page and recommend a non-authoritative edit workflow."""

    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        from atlassian_skills.confluence.page_inspect import inspect_page

        result = inspect_page(_make_client(ctx.obj), page_id, intent=intent)
        typer.echo(format_output(result, fmt))
    except AtlasError as error:
        _handle_error(error, fmt)


# ---------------------------------------------------------------------------
# page search <cql>
# ---------------------------------------------------------------------------


@page_app.command("search")
def page_search(
    ctx: typer.Context,
    cql: str = typer.Argument(..., help="CQL query string"),
    limit: int = typer.Option(25, "--limit", "-l", help="Max results"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Search Confluence pages with CQL."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = client.search(cql, limit=limit)
        typer.echo(format_output(result.results, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# page children <id>
# ---------------------------------------------------------------------------


@page_app.command("children")
def page_children(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Parent page ID"),
    limit: int = typer.Option(25, "--limit", "-l", help="Max results"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List child pages of a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        pages = client.get_children(page_id, limit=limit)
        typer.echo(format_output(pages, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# page history <id> <version>
# ---------------------------------------------------------------------------


@page_app.command("history")
def page_history(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    version: int = typer.Argument(..., help="Version number"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Get a specific historical version of a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        page = client.get_page_history(page_id, version)
        if fmt == OutputFormat.MD:
            from atlassian_skills.core.format.markdown import format_page_md_document, format_page_md_header

            conversion = confluence_storage_to_md_result(page.body_storage or "", profile="readable")
            space_key = page.space.key if page.space else ""
            header = format_page_md_header(page.title, space_key, page.version)
            typer.echo(format_page_md_document(header, conversion.markdown or ""))
            _emit_conversion_diagnostics(
                ctx,
                conversion.warnings,
                conversion.losses,
                conversion.push_safe,
            )
        else:
            typer.echo(format_output(page, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# page diff <id> --from <ver> --to <ver>
# ---------------------------------------------------------------------------


@page_app.command("diff")
def page_diff(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    from_ver: int = typer.Option(..., "--from", help="From version number"),
    to_ver: int = typer.Option(..., "--to", help="To version number"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Show unified diff between two page versions."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        diff = client.get_page_diff(page_id, from_ver, to_ver)
        if fmt == OutputFormat.JSON:
            typer.echo(json.dumps({"diff": diff}))
        else:
            typer.echo(diff)
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# page images <id>
# ---------------------------------------------------------------------------


@page_app.command("images")
def page_images(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List image attachments on a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        images = client.get_page_images(page_id)
        typer.echo(format_output(images, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# space tree <key>
# ---------------------------------------------------------------------------


@space_app.command("tree")
def space_tree(
    ctx: typer.Context,
    space_key: str = typer.Argument(..., help="Space key"),
    limit: int = typer.Option(200, "--limit", "-l", help="Max pages"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Get the page tree of a space."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = client.get_space_tree(space_key, limit=limit)
        typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# comment list <page_id>
# ---------------------------------------------------------------------------


@comment_app.command("list")
def comment_list(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List comments on a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        comments = client.list_comments(page_id)
        typer.echo(format_output(comments, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# label list <page_id>
# ---------------------------------------------------------------------------


@label_app.command("list")
def label_list(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List labels on a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        labels = client.list_labels(page_id)
        typer.echo(format_output(labels, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# attachment list <page_id>
# ---------------------------------------------------------------------------


@attachment_app.command("list")
def attachment_list(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List attachments on a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        attachments = client.list_attachments(page_id, limit=limit)
        typer.echo(format_output(attachments, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# attachment download <att_id> --output <path>
# ---------------------------------------------------------------------------


@attachment_app.command("download")
def attachment_download(
    ctx: typer.Context,
    att_id: str = typer.Argument(..., help="Attachment content ID"),
    output: str = typer.Option(..., "--output", "-o", help="Output file path"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Download a single attachment."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        path = client.download_attachment(att_id, output)
        typer.echo(format_output({"downloaded": str(path)}, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# attachment download-all <page_id> --output-dir <dir>
# ---------------------------------------------------------------------------


@attachment_app.command("download-all")
def attachment_download_all(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    output_dir: str = typer.Option(".", "--output-dir", "-o", help="Output directory"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Download all attachments from a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        paths = client.download_all_attachments(page_id, output_dir)
        data = [{"downloaded": str(p)} for p in paths]
        typer.echo(format_output(data, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# user search <query>
# ---------------------------------------------------------------------------


@user_app.command("search")
def user_search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Search query (fuzzy match on name/email)"),
    group: str = typer.Option("confluence-users", "--group", "-g", help="Group to search in"),
    limit: int = typer.Option(200, "--limit", "-l", help="Max group members to fetch"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Search Confluence users by name/email (fuzzy match on group members)."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        users = client.search_users(query, group_name=group, limit=limit)
        typer.echo(format_output(users, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@user_app.command("me")
def user_me(
    ctx: typer.Context,
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Get the current authenticated user."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        user = client.get_current_user()
        typer.echo(format_output(user, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ===========================================================================
# Write commands
# ===========================================================================


def _resolve_body(body_file: str | None, body_format: str) -> WriteConversionResult:
    """Read body from file/stdin and convert md to storage if needed."""
    if body_file is None:
        typer.echo("Error: --body-file is required for this command", err=True)
        raise typer.Exit(ExitCode.VALIDATION)
    raw = read_body(body_file=body_file)
    if body_format == "md":
        from atlassian_skills.confluence.push_md import _assert_push_safe_source

        _assert_push_safe_source(raw)
        result = md_to_confluence_storage_result(raw)
        if not result.push_safe or result.losses:
            raise ValidationError(
                "The Markdown body cannot be converted without losing content.",
                hint="Resolve the reported conversion losses before publishing.",
                context=_conversion_diagnostics(result.warnings, result.losses, result.push_safe),
            )
        return result
    return WriteConversionResult(body=raw)


# ---------------------------------------------------------------------------
# page create
# ---------------------------------------------------------------------------


@page_app.command("create")
def page_create(
    ctx: typer.Context,
    space: str = typer.Option(..., "--space", "-s", help="Space key"),
    title: str = typer.Option(..., "--title", "-t", help="Page title"),
    parent_id: str | None = typer.Option(None, "--parent-id", help="Parent page ID"),
    body_file: str | None = typer.Option(None, "--body-file", "-f", help="Body file path (- for stdin)"),
    body_format: str = typer.Option("storage", "--body-format", help="Body format: storage or md"),
    accept_conversion: str | None = typer.Option(
        None,
        "--accept-conversion",
        help="Exact source conversion fingerprint returned by preflight",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Create a new Confluence page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        if body_file is None:
            raise ValidationError("--body-file is required for this command", context={"reason": "body_file_required"})
        body = read_body(body_file=body_file)
        client = _make_client(ctx.obj)
        from atlassian_skills.confluence.stateless_write import create_page_stateless

        next_action = [
            "atls",
            "confluence",
            "page",
            "create",
            "--space",
            space,
            "--title",
            title,
            "--body-file",
            body_file,
            "--body-format",
            body_format,
        ]
        if parent_id is not None:
            next_action.extend(("--parent-id", parent_id))
        result = create_page_stateless(
            client,
            space=space,
            title=title,
            parent_id=parent_id,
            body=body,
            body_format=body_format,
            dry_run=dry_run,
            accept_conversion=accept_conversion,
            next_action_argv=tuple(next_action),
        )
        if fmt == OutputFormat.COMPACT and result["status"] == "created":
            typer.echo(format_output(WriteResult(action="created", key=str(result["id"]), summary=title), fmt))
        else:
            typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@page_app.command("copy")
def page_copy(
    ctx: typer.Context,
    source_page_id: str = typer.Argument(..., help="Source page ID (read-only)"),
    parent_id: str = typer.Option(..., "--parent-id", help="Destination parent page ID"),
    space: str = typer.Option(..., "--space", "-s", help="Destination space key"),
    title: str | None = typer.Option(None, "--title", "-t", help="Destination title (default: source title)"),
    include_attachments: bool = typer.Option(
        False,
        "--include-attachments",
        help="Copy every source attachment; required when attachments exist",
    ),
    verify: bool = typer.Option(
        True,
        "--verify/--no-verify",
        help="Download copied attachments and verify storage/attachment hashes",
    ),
    reason: str | None = typer.Option(None, "--reason", help="Add a visible comment to the copied page"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Read and preflight without creating content"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Copy one page and its attachments into a verified run-owned page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        result = copy_confluence_page(
            _make_client(ctx.obj),
            source_page_id,
            destination_parent_id=parent_id,
            destination_space=space,
            title=title,
            include_attachments=include_attachments,
            verify=verify,
            reason=reason,
            dry_run=dry_run,
        )
        if fmt == OutputFormat.COMPACT and result["status"] == "copied":
            typer.echo(
                format_output(
                    WriteResult(
                        action="copied",
                        key=str(result["target"]["id"]),
                        summary=str(result["target"]["title"]),
                    ),
                    fmt,
                )
            )
        else:
            typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# page update
# ---------------------------------------------------------------------------


@page_app.command("update")
def page_update(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID to update"),
    title: str | None = typer.Option(None, "--title", "-t", help="New title (default: keep current)"),
    body_file: str | None = typer.Option(None, "--body-file", "-f", help="Body file path (- for stdin)"),
    body_format: str = typer.Option("storage", "--body-format", help="Body format: storage or md"),
    if_version: int | None = typer.Option(None, "--if-version", help="Expected current version (stale check)"),
    reason: str | None = typer.Option(None, "--reason", help="Confluence version message"),
    minor_edit: bool = typer.Option(False, "--minor-edit", help="Mark the Confluence version as a minor edit"),
    accept_migration: str | None = typer.Option(
        None,
        "--accept-migration",
        help="Exact migration fingerprint returned by preflight",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Update an existing Confluence page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        if body_file is None:
            raise ValidationError("--body-file is required for this command", context={"reason": "body_file_required"})
        body = read_body(body_file=body_file)
        client = _make_client(ctx.obj)
        from atlassian_skills.confluence.stateless_write import build_page_update_preflight, publish_page_update

        preflight = build_page_update_preflight(
            client,
            page_id,
            body,
            body_format=body_format,
            title=title,
            if_version=if_version,
        )
        next_action = [
            "atls",
            "confluence",
            "page",
            "update",
            page_id,
            "--body-file",
            body_file,
            "--body-format",
            body_format,
        ]
        if title is not None:
            next_action.extend(("--title", title))
        if if_version is not None:
            next_action.extend(("--if-version", str(if_version)))
        if reason is not None:
            next_action.extend(("--reason", reason))
        if minor_edit:
            next_action.append("--minor-edit")
        if dry_run:
            dry_result = {**preflight.to_dict(), "status": "dry_run", "method": "PUT"}
            if preflight.consent_required:
                assert preflight.migration_fingerprint is not None
                dry_result["next_actions"] = [
                    consent_retry_action(
                        tuple(next_action),
                        option="--accept-migration",
                        fingerprint=preflight.migration_fingerprint,
                        description_code="REVIEW_MIGRATION_AND_RETRY",
                    )
                ]
            typer.echo(format_output(dry_result, fmt))
            return
        result = publish_page_update(
            client,
            preflight,
            accept_migration=accept_migration,
            reason=reason,
            minor_edit=minor_edit,
            next_action_argv=tuple(next_action),
        )
        if fmt == OutputFormat.COMPACT and result["status"] == "updated":
            output = WriteResult(action="updated", key=page_id)
            typer.echo(format_output(output, fmt))
        else:
            typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@page_app.command("patch-text")
def page_patch_text(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    find: str | None = typer.Option(None, "--find", help="Exact decoded plain text to find"),
    replace: str | None = typer.Option(None, "--replace", help="Replacement plain text"),
    patch_file: str | None = typer.Option(
        None,
        "--patch-file",
        help="JSON batch patch file with version and exact node selectors",
    ),
    if_version: int | None = typer.Option(None, "--if-version", help="Expected current version (stale check)"),
    reason: str | None = typer.Option(None, "--reason", help="Confluence version message"),
    minor_edit: bool = typer.Option(False, "--minor-edit", help="Mark the Confluence version as a minor edit"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and report exact text nodes without PUT"),
    format: str | None = typer.Option(None, "--format", help="Override output format (compact|json|md|raw)"),
) -> None:
    """Patch one or more exact storage text nodes with a state-free verified write."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        from atlassian_skills.confluence.patch_text import parse_patch_document, patch_text

        if patch_file is not None:
            if find is not None or replace is not None or if_version is not None:
                raise ValidationError(
                    "--patch-file cannot be combined with --find, --replace, or --if-version",
                    context={"reason": "patch_input_conflict"},
                )
            try:
                payload = json.loads(Path(patch_file).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise ValidationError(
                    "Unable to read a valid UTF-8 JSON patch file",
                    context={"reason": "patch_file_read_failed", "path": patch_file},
                ) from error
            document = parse_patch_document(payload)
        else:
            if find is None or replace is None:
                raise ValidationError(
                    "--find and --replace are required when --patch-file is omitted",
                    context={"reason": "patch_input_missing"},
                )
            document = None
            if if_version is None:
                raise ValidationError(
                    "patch-text requires --if-version when --patch-file is omitted",
                    context={"reason": "patch_version_required"},
                )

        client = _make_client(ctx.obj)
        result = patch_text(
            client,
            page_id,
            old=find,
            new=replace,
            patch_document=document,
            if_version=if_version,
            dry_run=dry_run,
            reason=reason,
            minor_edit=minor_edit,
        )
        typer.echo(format_output(result, fmt))
    except AtlasError as error:
        _handle_error(error, fmt)


# ---------------------------------------------------------------------------
# page delete
# ---------------------------------------------------------------------------


@page_app.command("delete")
def page_delete(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID to delete"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Delete a Confluence page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)

        if dry_run:
            typer.echo(format_dry_run("DELETE", f"{client.base_url}/rest/api/content/{page_id}", fmt=fmt.value))
            return

        client.delete_page(page_id)
        if fmt == OutputFormat.COMPACT:
            typer.echo(format_output(WriteResult(action="deleted", key=page_id), fmt))
        else:
            typer.echo(format_output({"deleted": page_id}, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# page move
# ---------------------------------------------------------------------------


@page_app.command("move")
def page_move(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID to move"),
    target: str = typer.Option(..., "--target", help="Target page ID"),
    position: str = typer.Option("append", "--position", "-p", help="Position: append, above, below"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Move a page relative to a target page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = client.move_page(page_id, position, target)
        if fmt == OutputFormat.COMPACT:
            typer.echo(format_output(WriteResult(action="moved", key=page_id), fmt))
        else:
            typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# comment add
# ---------------------------------------------------------------------------


@comment_app.command("add")
def comment_add(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    body_file: str | None = typer.Option(None, "--body-file", "-f", help="Body file path (- for stdin)"),
    body_format: str = typer.Option("storage", "--body-format", help="Body format: storage or md"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Add a comment to a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        body_conversion = _resolve_body(body_file, body_format)
        body = body_conversion.body
        client = _make_client(ctx.obj)

        if dry_run:
            payload = {
                "type": "comment",
                "container": {"id": page_id, "type": "page"},
                "body": {"storage": {"value": body, "representation": "storage"}},
            }
            _emit_conversion_diagnostics(
                ctx,
                body_conversion.warnings,
                body_conversion.losses,
                body_conversion.push_safe,
            )
            typer.echo(
                format_dry_run(
                    "POST",
                    f"{client.base_url}/rest/api/content",
                    body=payload,
                    fmt=fmt.value,
                )
            )
            return

        result = client.add_comment(page_id, body, body_format="storage")
        if fmt == OutputFormat.COMPACT:
            output = WriteResult(action="commented", key=page_id)
            typer.echo(format_output(_output_with_write_diagnostics(ctx, output, fmt, body_conversion), fmt))
        else:
            typer.echo(format_output(_output_with_write_diagnostics(ctx, result, fmt, body_conversion), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# comment reply
# ---------------------------------------------------------------------------


@comment_app.command("reply")
def comment_reply(
    ctx: typer.Context,
    comment_id: str = typer.Argument(..., help="Comment ID to reply to"),
    body_file: str | None = typer.Option(None, "--body-file", "-f", help="Body file path (- for stdin)"),
    body_format: str = typer.Option("storage", "--body-format", help="Body format: storage or md"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Reply to an existing comment."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        body_conversion = _resolve_body(body_file, body_format)
        body = body_conversion.body
        client = _make_client(ctx.obj)

        if dry_run:
            payload = {
                "type": "comment",
                "ancestors": [{"id": comment_id}],
                "body": {"storage": {"value": body, "representation": "storage"}},
            }
            _emit_conversion_diagnostics(
                ctx,
                body_conversion.warnings,
                body_conversion.losses,
                body_conversion.push_safe,
            )
            typer.echo(
                format_dry_run(
                    "POST",
                    f"{client.base_url}/rest/api/content",
                    body=payload,
                    fmt=fmt.value,
                )
            )
            return

        result = client.reply_to_comment(comment_id, body, body_format="storage")
        if fmt == OutputFormat.COMPACT:
            output = WriteResult(action="replied", key=str(result.get("id", comment_id)))
            typer.echo(format_output(_output_with_write_diagnostics(ctx, output, fmt, body_conversion), fmt))
        else:
            typer.echo(format_output(_output_with_write_diagnostics(ctx, result, fmt, body_conversion), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# label add
# ---------------------------------------------------------------------------


@label_app.command("add")
def label_add(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    labels: list[str] = typer.Argument(..., help="Labels to add"),  # noqa: B008
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Add labels to a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = client.add_label(page_id, labels)
        if fmt == OutputFormat.COMPACT:
            typer.echo(format_output(WriteResult(action="labeled", key=page_id, summary=",".join(labels)), fmt))
        else:
            typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# attachment upload
# ---------------------------------------------------------------------------


@attachment_app.command("upload")
def attachment_upload(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    file: str = typer.Argument(..., help="File path to upload"),
    comment: str | None = typer.Option(None, "--comment", "-c", help="Attachment comment"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Upload a single attachment to a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        path = Path(file)
        if not path.exists():
            typer.echo(f"Error: file not found: {file}", err=True)
            raise typer.Exit(1)
        client = _make_client(ctx.obj)
        result = client.upload_attachment(page_id, path, comment=comment)
        if fmt == OutputFormat.COMPACT:
            att_id = None
            if isinstance(result, dict):
                results_list = result.get("results") if isinstance(result.get("results"), list) else None
                first = results_list[0] if results_list else result
                if isinstance(first, dict) and first.get("id"):
                    att_id = str(first["id"])
            typer.echo(format_output(WriteResult(action="uploaded", key=page_id, id=att_id, summary=path.name), fmt))
        else:
            typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# attachment upload-batch
# ---------------------------------------------------------------------------


@attachment_app.command("upload-batch")
def attachment_upload_batch(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    files: list[str] = typer.Argument(..., help="File paths to upload"),  # noqa: B008
    if_exists: str = typer.Option("skip", "--if-exists", help="Behaviour for existing: skip, replace, version"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Upload multiple attachments to a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        for f in files:
            if not Path(f).exists():
                typer.echo(f"Error: file not found: {f}", err=True)
                raise typer.Exit(1)
        client = _make_client(ctx.obj)
        results = client.upload_attachments_batch(page_id, list(files), if_exists=if_exists)
        typer.echo(format_output(results, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# attachment delete
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# page push-md
# ---------------------------------------------------------------------------


@page_app.command("push-md")
def page_push_md(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    md_file: str = typer.Option(..., "--md-file", "-f", help="Managed Markdown file path; stdin is rejected"),
    passthrough_prefix: list[str] = typer.Option(
        [], "--passthrough-prefix", help="Passthrough prefixes (supported only on push-md/pull-md/diff-local)"
    ),  # noqa: B008
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    if_version: int | None = typer.Option(None, "--if-version", help="Expected current version (stale check)"),
    reason: str | None = typer.Option(None, "--reason", help="Confluence version message"),
    minor_edit: bool = typer.Option(False, "--minor-edit", help="Mark the Confluence version as a minor edit"),
    accept_migration: str | None = typer.Option(
        None,
        "--accept-migration",
        help="Exact migration fingerprint returned by the current preflight",
    ),
    format: str | None = typer.Option(None, "--format", help="Override output format (compact|json|md|raw)"),
) -> None:
    """Prove and publish a managed Markdown edit, then verify remote read-back."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        if md_file == "-":
            raise ValidationError(
                "Managed Confluence push requires --md-file PATH; stdin has no portable manifest identity.",
                context={"reason": "managed_file_required"},
            )
        md_path = Path(md_file)
        if not md_path.exists():
            raise NotFoundError(
                f"Managed Markdown file not found: {md_file}",
                context={"reason": "managed_file_not_found", "path": md_file},
            )

        md_content = read_body(body_file=md_file)
        client = _make_client(ctx.obj)

        from atlassian_skills.confluence.push_md import push_md

        next_action = ["atls", "confluence", "page", "push-md", page_id, "--md-file", md_file]
        for prefix in passthrough_prefix:
            next_action.extend(("--passthrough-prefix", prefix))
        if if_version is not None:
            next_action.extend(("--if-version", str(if_version)))
        if reason is not None:
            next_action.extend(("--reason", reason))
        if minor_edit:
            next_action.append("--minor-edit")

        result = push_md(
            client,
            page_id,
            md_content,
            passthrough_prefixes=passthrough_prefix or None,
            dry_run=dry_run,
            if_version=if_version,
            managed_path=md_path,
            reason=reason,
            minor_edit=minor_edit,
            accept_migration=accept_migration,
            next_action_argv=tuple(next_action),
        )
        conversion = result.get("conversion")
        if isinstance(conversion, dict) and fmt != OutputFormat.JSON:
            _emit_conversion_diagnostics(
                ctx,
                tuple(conversion.get("warnings", ())),
                tuple(conversion.get("losses", ())),
                bool(conversion.get("push_safe", True)),
            )
            result = {key: value for key, value in result.items() if key != "conversion"}
        typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@page_app.command("validate-local")
def page_validate_local(
    ctx: typer.Context,
    local_file: str = typer.Argument(..., help="Managed Markdown file path"),
    format: str | None = typer.Option(None, "--format", help="Override output format (compact|json|md|raw)"),
) -> None:
    """Validate a portable managed Markdown file without contacting Confluence."""

    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        from atlassian_skills.confluence.validate_local import validate_local

        typer.echo(format_output(validate_local(Path(local_file)), fmt))
    except AtlasError as error:
        _handle_error(error, fmt)


# ---------------------------------------------------------------------------
# page pull-md
# ---------------------------------------------------------------------------


@page_app.command("pull-md")
def page_pull_md(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    output: str = typer.Option(..., "--output", "-o", help="Required managed Markdown output file"),
    passthrough_prefix: list[str] = typer.Option(
        [], "--passthrough-prefix", help="Passthrough prefixes (supported only on push-md/pull-md/diff-local)"
    ),  # noqa: B008
    resolve_assets: str | None = typer.Option(None, "--resolve-assets", help="Asset resolution mode: sidecar"),
    asset_dir: str | None = typer.Option(None, "--asset-dir", help="Directory for resolved assets"),
    no_assets: bool = typer.Option(
        False, "--no-assets", help="Keep remote asset identity without local materialization"
    ),
    format: str | None = typer.Option(None, "--format", "-f", help="Override output format (compact|json|md|raw)"),
) -> None:
    """Publish a portable managed Markdown file with an embedded baseline."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        output_path = Path(output)

        from atlassian_skills.confluence.pull_md import pull_md

        result = pull_md(
            client,
            page_id,
            output_path=output_path,
            passthrough_prefixes=passthrough_prefix or None,
            resolve_assets=resolve_assets,
            asset_dir=Path(asset_dir) if asset_dir else None,
            site_url=getattr(client, "base_url", None),
            portable=True,
            no_assets=no_assets,
        )
        typer.echo(
            format_output(
                {
                    "status": result.status,
                    "path": str(output_path),
                    "version": result.version,
                    "assets": list(getattr(result, "assets", ())),
                    "edit_guidance": list(getattr(result, "edit_guidance", ())),
                    "migration_report": result.migration_report,
                    "migration_report_sha256": result.migration_report_sha256,
                    "conversion": {
                        **_conversion_diagnostics(result.warnings, result.losses, result.push_safe),
                        "blockers": list(result.blockers),
                    },
                },
                fmt,
            )
        )
        if fmt != OutputFormat.JSON:
            _emit_conversion_diagnostics(ctx, result.warnings, result.losses, result.push_safe)
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# page pull-batch
# ---------------------------------------------------------------------------


@page_app.command("pull-batch")
def page_pull_batch(
    ctx: typer.Context,
    page_ids: list[str] = typer.Argument(..., help="One or more Confluence page IDs"),
    output_dir: str = typer.Option(..., "--output-dir", help="Root directory for page folders"),
    passthrough_prefix: list[str] = typer.Option(
        [], "--passthrough-prefix", help="HTML comment prefixes to preserve during Markdown conversion"
    ),  # noqa: B008
    no_assets: bool = typer.Option(
        False, "--no-assets", help="Keep remote asset identity without local materialization"
    ),
    format: str | None = typer.Option(None, "--format", "-f", help="Override output format"),
) -> None:
    """Preflight and publish all managed pages/assets as one durable batch."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        from atlassian_skills.confluence.pull_md import pull_pages_batch

        results = pull_pages_batch(
            client,
            page_ids,
            Path(output_dir),
            passthrough_prefixes=passthrough_prefix or None,
            site_url=getattr(client, "base_url", None),
            portable=True,
            no_assets=no_assets,
        )
        typer.echo(
            format_output(
                [
                    {
                        "page_id": result.page_id,
                        "title": result.title,
                        "path": str(result.path),
                        "version": result.version,
                        "assets": result.assets,
                        "status": result.status,
                        "migration_report": result.migration_report,
                        "migration_report_sha256": result.migration_report_sha256,
                        "conversion": _conversion_diagnostics(
                            result.warnings,
                            result.losses,
                            result.push_safe,
                        )
                        | {"blockers": list(result.blockers)},
                    }
                    for result in results
                ],
                fmt,
            )
        )
        if fmt != OutputFormat.JSON:
            for result in results:
                _emit_conversion_diagnostics(ctx, result.warnings, result.losses, result.push_safe)
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# page diff-local
# ---------------------------------------------------------------------------


@page_app.command("diff-local")
def page_diff_local(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    local_file: str = typer.Argument(..., help="Local markdown file path"),
    passthrough_prefix: list[str] = typer.Option(
        [], "--passthrough-prefix", help="Passthrough prefixes (supported only on push-md/pull-md/diff-local)"
    ),  # noqa: B008
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Compare local markdown file vs server page content."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        local_path = Path(local_file)
        if not local_path.exists():
            typer.echo(f"Error: file not found: {local_file}", err=True)
            raise typer.Exit(1)

        client = _make_client(ctx.obj)

        from atlassian_skills.core.managed_file import read_managed_utf8
        from atlassian_skills.core.managed_manifest import ManagedManifestError, parse_managed_manifest

        local_markdown = read_managed_utf8(local_path, reason="local_markdown_read_failed")
        legacy_manifest = False
        try:
            parse_managed_manifest(local_markdown)
        except ManagedManifestError as manifest_error:
            portable_managed = False
            legacy_manifest = manifest_error.reason == "legacy_binding_marker"
        else:
            portable_managed = True
        if portable_managed:
            import difflib

            from atlassian_skills.confluence.migration_preflight import build_managed_preflight

            managed_preflight = build_managed_preflight(
                client,
                page_id,
                local_path,
                passthrough_prefixes=tuple(passthrough_prefix) if passthrough_prefix else None,
            )
            diff = "".join(
                difflib.unified_diff(
                    managed_preflight.base_markdown.splitlines(keepends=True),
                    managed_preflight.edited_markdown.splitlines(keepends=True),
                    fromfile="base",
                    tofile="local",
                )
            )
            identical = not managed_preflight.would_update
            typer.echo(format_output({**managed_preflight.to_dict(), "diff": diff, "identical": identical}, fmt))
            if not identical:
                raise typer.Exit(1)
            return

        if legacy_manifest:
            raise ValidationError(
                "Legacy managed Markdown must be re-pulled into the portable v2 format",
                context={
                    "reason": "legacy_manifest_repull_required",
                    "path": str(local_path),
                    "page_id": page_id,
                },
            )

        from atlassian_skills.confluence.diff_local import diff_local

        result = diff_local(client, page_id, local_path, passthrough_prefixes=passthrough_prefix or None)
        conversion = _conversion_diagnostics(result.warnings, result.losses, result.push_safe)
        if result.exit_code == 0:
            if fmt == OutputFormat.JSON:
                typer.echo(json.dumps({"identical": True, "conversion": conversion}))
            else:
                typer.echo("Identical (no differences)")
        else:
            if fmt == OutputFormat.JSON:
                typer.echo(json.dumps({"identical": False, "diff": result.diff_output, "conversion": conversion}))
            else:
                typer.echo(result.diff_output)
        if fmt != OutputFormat.JSON:
            _emit_conversion_diagnostics(ctx, result.warnings, result.losses, result.push_safe)
        raise typer.Exit(result.exit_code)
    except AtlasError as e:
        _handle_error(e, fmt)


@attachment_app.command("delete")
def attachment_delete(
    ctx: typer.Context,
    att_id: str = typer.Argument(..., help="Attachment content ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Delete a single attachment."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        client.delete_attachment(att_id)
        typer.echo(format_output({"deleted": att_id}, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)
