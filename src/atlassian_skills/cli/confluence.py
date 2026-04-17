from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import typer

from atlassian_skills.confluence.client import ConfluenceClient
from atlassian_skills.confluence.models import PageVersion
from atlassian_skills.core.auth import resolve_credential
from atlassian_skills.core.config import get_profile, load_config
from atlassian_skills.core.dryrun import format_dry_run
from atlassian_skills.core.errors import AtlasError, ExitCode
from atlassian_skills.core.format import OutputFormat, format_output
from atlassian_skills.core.format.markdown import confluence_storage_to_md, md_to_confluence_storage
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


def _handle_error(err: AtlasError, fmt: OutputFormat) -> None:
    if fmt == OutputFormat.JSON:
        typer.echo(json.dumps(err.to_dict()))
    else:
        typer.echo(f"Error: {err.message}", err=True)
        if err.hint:
            typer.echo(f"Hint:  {err.hint}", err=True)
    raise typer.Exit(err.exit_code)


# ---------------------------------------------------------------------------
# page get <id>
# ---------------------------------------------------------------------------


@page_app.command("get")
def page_get(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    body_repr: str | None = typer.Option(None, "--body-repr", help="Body representation: md|raw|storage"),
    format: str | None = typer.Option(None, "--format", "-f", help="Override output format"),
) -> None:
    """Get a Confluence page by ID."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)

    # Task 2: Expand minimization — skip body when compact and no body-repr.
    needs_body = body_repr in ("md", "raw", "storage") or fmt in (OutputFormat.MD, OutputFormat.RAW)
    include_body = needs_body or fmt not in (OutputFormat.COMPACT,)

    try:
        client = _make_client(ctx.obj)

        # RAW format: return server response text verbatim (byte-preserving contract)
        if fmt == OutputFormat.RAW:
            typer.echo(client.get_page_raw_text(page_id))
            return

        page = client.get_page(page_id, include_body=include_body)

        if body_repr == "md" and page.body_storage:
            page.body_storage = confluence_storage_to_md(page.body_storage)
        # "raw" and "storage" keep the storage XHTML as-is

        # When body_repr is specified and fmt=MD, bypass format_output to prevent
        # double conversion (body_repr already set the body representation).
        if fmt == OutputFormat.MD and body_repr:
            from atlassian_skills.core.format.markdown import format_page_md_header

            space_key = page.space.key if page.space else ""
            header = format_page_md_header(page.title, space_key, page.version)
            typer.echo(header + (page.body_storage or ""))
        else:
            typer.echo(format_output(page, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


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


def _resolve_body(body_file: str | None, body_format: str) -> str:
    """Read body from file/stdin and convert md to storage if needed."""
    if body_file is None:
        typer.echo("Error: --body-file is required for this command", err=True)
        raise typer.Exit(ExitCode.VALIDATION)
    raw = read_body(body_file=body_file)
    if body_format == "md":
        return md_to_confluence_storage(raw)
    return raw


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
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Create a new Confluence page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        body = _resolve_body(body_file, body_format)
        client = _make_client(ctx.obj)

        if dry_run:
            payload = {
                "type": "page",
                "title": title,
                "space": {"key": space},
                "body": {"storage": {"value": body, "representation": "storage"}},
            }
            if parent_id:
                ancestors: list[dict[str, str]] = [{"id": parent_id}]
                payload["ancestors"] = ancestors  # type: ignore[assignment]
            typer.echo(format_dry_run("POST", f"{client.base_url}/rest/api/content", body=payload, fmt=fmt.value))
            return

        result = client.create_page(space, title, body, ancestor_id=parent_id, body_format="storage")
        if fmt == OutputFormat.COMPACT:
            page_id = result.id if hasattr(result, "id") else result.get("id", "") if isinstance(result, dict) else ""
            typer.echo(format_output(WriteResult(action="created", key=str(page_id), summary=title), fmt))
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
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Update an existing Confluence page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        body = _resolve_body(body_file, body_format)
        client = _make_client(ctx.obj)

        # Fetch current page to get version and title
        current = client.get_page(page_id)
        current_version = current.version.number if isinstance(current.version, PageVersion) else 1
        current_title = current.title

        # Stale check
        if if_version is not None and current_version != if_version:
            typer.echo(
                f"Error: version mismatch (expected {if_version}, got {current_version})",
                err=True,
            )
            raise typer.Exit(ExitCode.STALE)

        new_version = current_version + 1
        new_title = title if title is not None else current_title

        if dry_run:
            payload = {
                "type": "page",
                "title": new_title,
                "body": {"storage": {"value": body, "representation": "storage"}},
                "version": {"number": new_version},
            }
            typer.echo(
                format_dry_run(
                    "PUT",
                    f"{client.base_url}/rest/api/content/{page_id}",
                    body=payload,
                    fmt=fmt.value,
                )
            )
            return

        result = client.update_page(page_id, new_title, body, new_version, body_format="storage")
        if fmt == OutputFormat.COMPACT:
            typer.echo(format_output(WriteResult(action="updated", key=page_id), fmt))
        else:
            typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


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
        body = _resolve_body(body_file, body_format)
        client = _make_client(ctx.obj)

        if dry_run:
            payload = {
                "type": "comment",
                "container": {"id": page_id, "type": "page"},
                "body": {"storage": {"value": body, "representation": "storage"}},
            }
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
            typer.echo(format_output(WriteResult(action="commented", key=page_id), fmt))
        else:
            typer.echo(format_output(result, fmt))
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
        body = _resolve_body(body_file, body_format)
        client = _make_client(ctx.obj)

        if dry_run:
            payload = {
                "type": "comment",
                "ancestors": [{"id": comment_id}],
                "body": {"storage": {"value": body, "representation": "storage"}},
            }
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
        typer.echo(format_output(result, fmt))
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
    md_file: str = typer.Option(..., "--md-file", "-f", help="Path to markdown file ('-' reads stdin)"),
    passthrough_prefix: list[str] = typer.Option(
        [], "--passthrough-prefix", help="Passthrough prefixes (supported only on push-md/pull-md/diff-local)"
    ),  # noqa: B008
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    attachment: list[str] = typer.Option([], "--attachment", help="Attachment file paths"),  # noqa: B008
    asset_dir: str | None = typer.Option(
        None, "--asset-dir", help="Directory of files to attach (missing directories are treated as empty)"
    ),
    attachment_if_exists: str = typer.Option("replace", "--attachment-if-exists", help="skip or replace"),
    if_version: int | None = typer.Option(None, "--if-version", help="Expected current version (stale check)"),
    format: str | None = typer.Option(None, "--format", help="Override output format (compact|json|md|raw)"),
) -> None:
    """Push local markdown file to a Confluence page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        md_path = None if md_file == "-" else Path(md_file)
        if md_path is not None and not md_path.exists():
            typer.echo(f"Error: file not found: {md_file}", err=True)
            raise typer.Exit(1)

        md_content = read_body(body_file=md_file)
        client = _make_client(ctx.obj)

        # Build attachment list: explicit --attachment + --asset-dir expansion
        att_paths: list[Path] = [Path(a) for a in attachment]
        if asset_dir:
            ad = Path(asset_dir)
            if not ad.exists():
                if not ctx.obj.get("quiet"):
                    typer.echo(f"# skipping missing asset-dir: {asset_dir}", err=True)
            elif not ad.is_dir():
                typer.echo(f"Error: not a directory: {asset_dir}", err=True)
                raise typer.Exit(1)
            else:
                att_paths.extend(sorted(p for p in ad.iterdir() if p.is_file() and not p.name.startswith(".")))

        from atlassian_skills.confluence.push_md import push_md

        result = push_md(
            client,
            page_id,
            md_content,
            passthrough_prefixes=passthrough_prefix or None,
            dry_run=dry_run,
            attachments=att_paths or None,
            attachment_if_exists=attachment_if_exists,
            if_version=if_version,
        )
        typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# page pull-md
# ---------------------------------------------------------------------------


@page_app.command("pull-md")
def page_pull_md(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    output: str | None = typer.Option(None, "--output", "-o", help="Output file path"),
    passthrough_prefix: list[str] = typer.Option(
        [], "--passthrough-prefix", help="Passthrough prefixes (supported only on push-md/pull-md/diff-local)"
    ),  # noqa: B008
    resolve_assets: str | None = typer.Option(None, "--resolve-assets", help="Asset resolution mode: sidecar"),
    asset_dir: str | None = typer.Option(None, "--asset-dir", help="Directory for resolved assets"),
    format: str | None = typer.Option(None, "--format", "-f", help="Override output format (compact|json|md|raw)"),
) -> None:
    """Pull a Confluence page as markdown."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        output_path = Path(output) if output else None

        from atlassian_skills.confluence.pull_md import pull_md

        result = pull_md(
            client,
            page_id,
            output_path=output_path,
            passthrough_prefixes=passthrough_prefix or None,
            resolve_assets=resolve_assets,
            asset_dir=Path(asset_dir) if asset_dir else None,
        )
        if output_path:
            typer.echo(format_output({"status": "written", "path": str(output_path), "version": result.version}, fmt))
        elif fmt == OutputFormat.JSON:
            typer.echo(
                format_output({"markdown": result.markdown, "version": result.version, "title": result.title}, fmt)
            )
        else:
            typer.echo(result.markdown)
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

        from atlassian_skills.confluence.diff_local import diff_local

        exit_code, diff_output = diff_local(
            client, page_id, local_path, passthrough_prefixes=passthrough_prefix or None
        )
        if exit_code == 0:
            if fmt == OutputFormat.JSON:
                typer.echo(json.dumps({"identical": True}))
            else:
                typer.echo("Identical (no differences)")
        else:
            if fmt == OutputFormat.JSON:
                typer.echo(json.dumps({"identical": False, "diff": diff_output}))
            else:
                typer.echo(diff_output)
        raise typer.Exit(exit_code)
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
