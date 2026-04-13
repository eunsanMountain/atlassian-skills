from __future__ import annotations

import json
import os
from typing import Any

import typer

from atlassian_skills.core.auth import resolve_credential
from atlassian_skills.core.config import get_profile, load_config
from atlassian_skills.core.dryrun import format_dry_run
from atlassian_skills.core.errors import AtlasError, ExitCode, ValidationError
from atlassian_skills.core.format import OutputFormat, format_output
from atlassian_skills.core.format.markdown import _SectionNotFoundError, jira_wiki_to_md, jira_wiki_to_md_with_options
from atlassian_skills.core.stdin import read_body
from atlassian_skills.jira.client import JiraClient
from atlassian_skills.jira.models import Issue

jira_app = typer.Typer(help="Jira commands", no_args_is_help=True)

# Sub-groups
user_app = typer.Typer(help="User commands", no_args_is_help=True)
issue_app = typer.Typer(help="Issue commands", no_args_is_help=True)
field_app = typer.Typer(help="Field commands", no_args_is_help=True)
project_app = typer.Typer(help="Project commands", no_args_is_help=True)
board_app = typer.Typer(help="Board commands", no_args_is_help=True)
sprint_app = typer.Typer(help="Sprint commands", no_args_is_help=True)
dev_info_app = typer.Typer(help="Dev-info commands", no_args_is_help=True)
link_app = typer.Typer(help="Link commands", no_args_is_help=True)
worklog_app = typer.Typer(help="Worklog commands", no_args_is_help=True)
watcher_app = typer.Typer(help="Watcher commands", no_args_is_help=True)
attachment_app = typer.Typer(help="Attachment commands", no_args_is_help=True)
sd_app = typer.Typer(help="Service-desk commands", no_args_is_help=True)

comment_app = typer.Typer(help="Comment commands", no_args_is_help=True)
epic_app = typer.Typer(help="Epic commands", no_args_is_help=True)
issue_batch_app = typer.Typer(help="Issue batch commands", no_args_is_help=True)

jira_app.add_typer(user_app, name="user")
jira_app.add_typer(issue_app, name="issue")
jira_app.add_typer(issue_batch_app, name="issue-batch")
jira_app.add_typer(field_app, name="field")
jira_app.add_typer(project_app, name="project")
jira_app.add_typer(board_app, name="board")
jira_app.add_typer(sprint_app, name="sprint")
jira_app.add_typer(dev_info_app, name="dev-info")
jira_app.add_typer(link_app, name="link")
jira_app.add_typer(comment_app, name="comment")
jira_app.add_typer(worklog_app, name="worklog")
jira_app.add_typer(watcher_app, name="watcher")
jira_app.add_typer(epic_app, name="epic")
jira_app.add_typer(attachment_app, name="attachment")
jira_app.add_typer(sd_app, name="service-desk")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(ctx_obj: dict[str, Any]) -> JiraClient:
    profile_name: str = ctx_obj.get("profile", "default")
    timeout: float = ctx_obj.get("timeout", 30.0)
    config = load_config()
    profile = get_profile(config, profile_name)
    url = profile.jira_url or os.environ.get(f"ATLS_{profile_name.upper()}_JIRA_URL")
    if not url:
        typer.echo(
            f"No Jira URL for profile '{profile_name}'. "
            f"Set jira_url in config or ATLS_{profile_name.upper()}_JIRA_URL env var.",
            err=True,
        )
        raise typer.Exit(1)
    credential = resolve_credential(profile_name, "jira", profile)
    verify: str | bool = profile.ca_bundle if profile.ca_bundle else True
    return JiraClient(url.rstrip("/"), credential, timeout=timeout, verify=verify)


def _fmt(ctx_obj: dict[str, Any]) -> OutputFormat:
    fmt = ctx_obj.get("format", OutputFormat.COMPACT)
    return OutputFormat(fmt) if not isinstance(fmt, OutputFormat) else fmt


def _resolve_fmt(ctx_obj: dict[str, Any], local_format: str | None) -> OutputFormat:
    return OutputFormat(local_format) if local_format else _fmt(ctx_obj)


def _handle_error(err: AtlasError, fmt: OutputFormat) -> None:
    if fmt == OutputFormat.JSON:
        typer.echo(json.dumps(err.to_dict()))
    else:
        typer.echo(f"Error: {err.message}", err=True)
        if err.hint:
            typer.echo(f"Hint:  {err.hint}", err=True)
    raise typer.Exit(err.exit_code)


def _issue_to_compact_dict(issue: Issue) -> dict[str, Any]:
    return {
        "key": issue.key,
        "status": issue.status.name if issue.status else "",
        "issuetype": issue.issue_type.name if issue.issue_type else "",
        "priority": issue.priority.name if issue.priority else "",
        "assignee": issue.assignee.display_name if issue.assignee else "",
        "summary": issue.summary or "",
        "updated": issue.updated or "",
    }


def _issue_to_json_dict(issue: Issue) -> dict[str, Any]:
    data = issue.model_dump()
    for key, value in issue.custom_fields.items():
        data[key] = value
    return data


def _render_issue(issue: Issue, fmt: OutputFormat) -> str:
    if fmt in (OutputFormat.JSON, OutputFormat.MD):
        return format_output(_issue_to_json_dict(issue), fmt)
    return format_output(_issue_to_compact_dict(issue), fmt)


def _render_issue_list(issues: list[Issue], fmt: OutputFormat) -> str:
    if fmt in (OutputFormat.JSON, OutputFormat.MD):
        return format_output([_issue_to_json_dict(i) for i in issues], fmt)
    return format_output([_issue_to_compact_dict(i) for i in issues], fmt)


def _parse_customfield_updates(values: list[str] | None) -> dict[str, str]:
    updates: dict[str, str] = {}
    if not values:
        return updates
    for pair in values:
        key, sep, value = pair.partition("=")
        field_id = key.strip()
        if not sep or not field_id:
            raise ValidationError(f"Invalid --set-customfield value: {pair!r}. Expected customfield_ID=value")
        updates[field_id] = value.strip()
    return updates


def _customfield_value_matches(actual: Any, expected: str) -> bool:
    if actual is None:
        return False
    if isinstance(actual, list):
        return any(_customfield_value_matches(item, expected) for item in actual)
    if isinstance(actual, dict):
        for key in ("key", "value", "name", "id"):
            candidate = actual.get(key)
            if candidate is not None and str(candidate) == expected:
                return True
        return False
    return str(actual) == expected


def _verify_customfield_updates(client: JiraClient, key: str, expected_fields: dict[str, str]) -> None:
    if not expected_fields:
        return
    raw_issue = client.get_issue_raw(key, fields=list(expected_fields))
    raw_fields = raw_issue.get("fields", {})
    mismatches: dict[str, Any] = {}
    for field_id, expected in expected_fields.items():
        actual = raw_fields.get(field_id)
        if not _customfield_value_matches(actual, expected):
            mismatches[field_id] = {"expected": expected, "actual": actual}
    if mismatches:
        mismatch_keys = ", ".join(sorted(mismatches))
        raise ValidationError(
            f"Custom field update was not applied for: {mismatch_keys}",
            hint="Use --fields-json for structured custom fields, or verify the Jira field expects a simple key/string value.",
            context={"mismatches": mismatches},
        )


# ---------------------------------------------------------------------------
# user get <id>
# ---------------------------------------------------------------------------


@user_app.command("get")
def user_get(
    ctx: typer.Context,
    username: str = typer.Argument(..., help="Jira username / account key"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Get a Jira user profile."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        user = client.get_user(username)
        typer.echo(format_output(user.model_dump(), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# issue subcommands
# ---------------------------------------------------------------------------


@issue_app.command("get")
def issue_get(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key, e.g. PROJ-1"),
    fields: str | None = typer.Option(None, "--fields", help="Comma-separated fields"),
    format: str | None = typer.Option(None, "--format", "-f", help="Override output format"),
    body_repr: str | None = typer.Option(None, "--body-repr", help="Body representation: md|raw|wiki"),
    section: str | None = typer.Option(None, "--section", help="Extract specific H2 section from body"),
    heading_promotion: str | None = typer.Option(
        None, "--heading-promotion", help="Heading promotion: confluence|jira|none (future)"
    ),
    drop_leading_notice: str | None = typer.Option(
        None, "--drop-leading-notice", help="Comma-separated notice prefixes to strip"
    ),
) -> None:
    """Get a single Jira issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    field_list = [f.strip() for f in fields.split(",") if f.strip()] if fields else ctx.obj.get("fields")
    notice_prefixes = [p.strip() for p in drop_leading_notice.split(",") if p.strip()] if drop_leading_notice else None

    # Task 2: Expand minimization — skip description when fields excludes it.
    _needs_body = body_repr in ("md", "raw", "wiki") or fmt in (OutputFormat.MD, OutputFormat.RAW)
    if field_list and "description" not in field_list:
        _needs_body = False

    try:
        client = _make_client(ctx.obj)
        issue = client.get_issue(key, fields=field_list)

        # Task 1: --body-repr conversion on the description field.
        if body_repr and issue.description:
            if body_repr == "md":
                issue.description = jira_wiki_to_md(issue.description)
            # "raw" and "wiki" keep the original wiki markup (Server stores wiki natively)

        if fmt == OutputFormat.MD and (section or heading_promotion or notice_prefixes):
            description_raw = issue.description or ""
            try:
                body_md = jira_wiki_to_md_with_options(
                    description_raw,
                    section=section,
                    heading_promotion=heading_promotion,
                    drop_leading_notice=notice_prefixes,
                )
            except _SectionNotFoundError as exc:
                raise ValidationError(f"Section '{exc.section}' not found in issue body") from exc
            typer.echo(body_md)
        else:
            typer.echo(_render_issue(issue, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@issue_app.command("search")
def issue_search(
    ctx: typer.Context,
    jql: str = typer.Argument(..., help="JQL query string"),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results"),
    fields: str | None = typer.Option(None, "--fields", help="Comma-separated fields"),
    format: str | None = typer.Option(None, "--format", "-f", help="Override output format"),
    section: str | None = typer.Option(None, "--section", help="Extract specific H2 section from body"),
    heading_promotion: str | None = typer.Option(
        None, "--heading-promotion", help="Heading promotion: confluence|jira|none (future)"
    ),
    drop_leading_notice: str | None = typer.Option(
        None, "--drop-leading-notice", help="Comma-separated notice prefixes to strip"
    ),
) -> None:
    """Search issues with JQL."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    field_list = [f.strip() for f in fields.split(",") if f.strip()] if fields else ctx.obj.get("fields")
    notice_prefixes = [p.strip() for p in drop_leading_notice.split(",") if p.strip()] if drop_leading_notice else None
    try:
        client = _make_client(ctx.obj)
        result = client.search(jql, fields=field_list, max_results=limit)
        if fmt == OutputFormat.MD and (section or heading_promotion or notice_prefixes):
            parts: list[str] = []
            for issue in result.issues:
                description_raw = issue.description or ""
                try:
                    body_md = jira_wiki_to_md_with_options(
                        description_raw,
                        section=section,
                        heading_promotion=heading_promotion,
                        drop_leading_notice=notice_prefixes,
                    )
                except _SectionNotFoundError as exc:
                    raise ValidationError(f"Section '{exc.section}' not found in issue '{issue.key}' body") from exc
                parts.append(f"# {issue.key}: {issue.summary or ''}\n\n{body_md}")
            typer.echo("\n\n---\n\n".join(parts))
        else:
            typer.echo(_render_issue_list(result.issues, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@issue_app.command("transitions")
def issue_transitions(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List available transitions for an issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        transitions = client.get_transitions(key)
        data = [t.model_dump() for t in transitions]
        typer.echo(format_output(data, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@issue_app.command("dates")
def issue_dates(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Get date fields (created, updated, due, resolution) for an issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        dates = client.get_issue_dates(key)
        typer.echo(format_output(dates, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@issue_app.command("sla")
def issue_sla(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Get SLA information for a service desk request."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        data = client.get_issue_sla(key)
        typer.echo(format_output(data, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@issue_app.command("images")
def issue_images(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List image attachments on an issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        images = client.get_issue_images(key)
        typer.echo(format_output(images, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# field subcommands
# ---------------------------------------------------------------------------


@field_app.command("search")
def field_search(
    ctx: typer.Context,
    keyword: str | None = typer.Argument(None, help="Optional keyword filter"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List/search Jira fields."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        fields = client.search_fields(keyword)
        data = [f.model_dump() for f in fields]
        typer.echo(format_output(data, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@field_app.command("options")
def field_options(
    ctx: typer.Context,
    field_id: str = typer.Argument(..., help="Field ID"),
    project: str = typer.Option(..., "--project", help="Project key"),
    issue_type: str = typer.Option(..., "--issue-type", help="Issue type name"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Get allowed values for a field in a project/issue-type context."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        options = client.get_field_options(field_id, project, issue_type)
        typer.echo(format_output(options, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# project subcommands
# ---------------------------------------------------------------------------


@project_app.command("list")
def project_list(
    ctx: typer.Context,
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List all accessible Jira projects."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        projects = client.list_projects()
        data = [p.model_dump() for p in projects]
        typer.echo(format_output(data, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@project_app.command("issues")
def project_issues(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Project key"),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List issues in a project."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = client.get_project_issues(key, limit=limit)
        typer.echo(_render_issue_list(result.issues, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@project_app.command("versions")
def project_versions(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Project key"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List versions (fix versions) for a project."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        versions = client.get_project_versions(key)
        data = [v.model_dump() for v in versions]
        typer.echo(format_output(data, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@project_app.command("components")
def project_components(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Project key"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List components for a project."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        components = client.get_project_components(key)
        data = [c.model_dump() for c in components]
        typer.echo(format_output(data, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# board subcommands
# ---------------------------------------------------------------------------


@board_app.command("list")
def board_list(
    ctx: typer.Context,
    name: str | None = typer.Option(None, "--name", help="Filter by board name"),
    project: str | None = typer.Option(None, "--project", help="Filter by project key"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List Jira agile boards."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        boards = client.list_boards(name=name, project=project)
        typer.echo(format_output(boards, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@board_app.command("issues")
def board_issues(
    ctx: typer.Context,
    board_id: int = typer.Argument(..., help="Board ID"),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List issues on a board."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        issues = client.get_board_issues(board_id, limit=limit)
        typer.echo(_render_issue_list(issues, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# sprint subcommands
# ---------------------------------------------------------------------------


@sprint_app.command("list")
def sprint_list(
    ctx: typer.Context,
    board_id: int = typer.Argument(..., help="Board ID"),
    state: str | None = typer.Option(None, "--state", help="Filter by state: active|future|closed"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List sprints on a board."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        sprints = client.list_sprints(board_id, state=state)
        typer.echo(format_output(sprints, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@sprint_app.command("issues")
def sprint_issues(
    ctx: typer.Context,
    sprint_id: int = typer.Argument(..., help="Sprint ID"),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List issues in a sprint."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = client.get_sprint_issues(sprint_id, limit=limit)
        typer.echo(_render_issue_list(result.issues, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# dev-info subcommands
# ---------------------------------------------------------------------------


@dev_info_app.command("get")
def dev_info_get(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key or ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Get dev info (branches, PRs, commits) for an issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        data = client.get_dev_info(key)
        typer.echo(format_output(data, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@dev_info_app.command("get-many")
def dev_info_get_many(
    ctx: typer.Context,
    keys: list[str] = typer.Argument(..., help="Issue keys (space-separated)"),  # noqa: B008
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Get dev info summary for multiple issues."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        data = client.get_dev_info_many(keys)
        typer.echo(format_output(data, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# link list-types
# ---------------------------------------------------------------------------


@link_app.command("list-types")
def link_list_types(
    ctx: typer.Context,
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List all issue link types."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        link_types = client.list_link_types()
        data = [lt.model_dump() for lt in link_types]
        typer.echo(format_output(data, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# worklog list <key>
# ---------------------------------------------------------------------------


@worklog_app.command("list")
def worklog_list(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List worklogs for an issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        wl = client.list_worklogs(key)
        typer.echo(format_output(wl.model_dump(), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# watcher list <key>
# ---------------------------------------------------------------------------


@watcher_app.command("list")
def watcher_list(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List watchers for an issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        wl = client.list_watchers(key)
        typer.echo(format_output(wl.model_dump(), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# attachment list <key>
# ---------------------------------------------------------------------------


@attachment_app.command("list")
def attachment_list(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List attachments for an issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        attachments = client.get_attachment_content(key)
        typer.echo(format_output(attachments, fmt), err=False)
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# attachment download <key> [--output-dir]
# ---------------------------------------------------------------------------


@attachment_app.command("download")
def attachment_download(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    output_dir: str = typer.Option(".", "--output-dir", "-o", help="Directory to save attachments"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Download attachments for an issue (not yet implemented — currently lists only)."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        attachments = client.get_attachment_content(key)
        typer.echo(format_output(attachments, fmt), err=False)
        if not ctx.obj.get("quiet"):
            typer.echo(f"# output-dir: {output_dir} (download not yet implemented)", err=True)
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# service-desk subcommands
# ---------------------------------------------------------------------------


@sd_app.command("list")
def sd_list(
    ctx: typer.Context,
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List all service desks."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        data = client.list_service_desks()
        typer.echo(format_output(data, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@sd_app.command("queues")
def sd_queues(
    ctx: typer.Context,
    sd_id: int = typer.Argument(..., help="Service desk ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List queues in a service desk."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        data = client.get_service_desk_queues(sd_id)
        typer.echo(format_output(data, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@sd_app.command("queue-issues")
def sd_queue_issues(
    ctx: typer.Context,
    sd_id: int = typer.Argument(..., help="Service desk ID"),
    queue_id: int = typer.Argument(..., help="Queue ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List issues in a service desk queue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        data = client.get_queue_issues(sd_id, queue_id)
        typer.echo(format_output(data, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ===========================================================================
# WRITE COMMANDS
# ===========================================================================


# ---------------------------------------------------------------------------
# issue create
# ---------------------------------------------------------------------------


@issue_app.command("create")
def issue_create(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", "-p", help="Project key"),
    type: str = typer.Option(..., "--type", "-t", help="Issue type name"),
    summary: str = typer.Option(..., "--summary", "-s", help="Issue summary"),
    body_file: str | None = typer.Option(None, "--body-file", help="Description body file (- for stdin)"),
    body_format: str | None = typer.Option(None, "--body-format", help="Body format hint"),
    fields_json: str | None = typer.Option(None, "--fields-json", help="Extra fields as JSON string"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be sent"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Create a new Jira issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        fields: dict[str, Any] = {
            "project": {"key": project},
            "issuetype": {"name": type},
            "summary": summary,
        }
        if body_file:
            body_text = read_body(body_file=body_file)
            if body_format == "md":
                from atlassian_skills.core.format.markdown import md_to_jira_wiki

                body_text = md_to_jira_wiki(body_text)
            fields["description"] = body_text
        if fields_json:
            try:
                fields.update(json.loads(fields_json))
            except json.JSONDecodeError as exc:
                raise ValidationError(f"Invalid --fields-json: {exc}") from exc

        if dry_run:
            client = _make_client(ctx.obj)
            typer.echo(format_dry_run("POST", f"{client.base_url}/rest/api/2/issue", body={"fields": fields}))
            return

        client = _make_client(ctx.obj)
        result = client.create_issue(fields)
        typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# issue update
# ---------------------------------------------------------------------------


@issue_app.command("update")
def issue_update(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    body_file: str | None = typer.Option(None, "--body-file", help="Description body file (- for stdin)"),
    body_format: str | None = typer.Option(None, "--body-format", help="Body format hint"),
    fields_json: str | None = typer.Option(None, "--fields-json", help="Fields as JSON string"),
    set_customfield: list[str] | None = typer.Option(None, "--set-customfield", help="KEY=VAL custom field (read-back verified; use --fields-json for structured values)"),  # noqa: B008
    if_updated: str | None = typer.Option(None, "--if-updated", help="ISO8601 timestamp for stale check"),
    heading_promotion: str | None = typer.Option(None, "--heading-promotion", help="Heading promotion: jira|confluence|none"),
    passthrough_prefix: list[str] = typer.Option([], "--passthrough-prefix", help="Passthrough prefixes for md→wiki conversion only"),  # noqa: B008
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be sent"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Update an existing Jira issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)

        # Stale check: compare --if-updated with the issue's updated field
        if if_updated is not None:
            issue = client.get_issue(key)
            server_updated = issue.updated or ""
            if server_updated != if_updated:
                typer.echo(
                    f"Error: stale issue (expected updated={if_updated}, got {server_updated})",
                    err=True,
                )
                raise typer.Exit(ExitCode.STALE)

        fields: dict[str, Any] = {}
        if body_file:
            body_text = read_body(body_file=body_file)
            if body_format == "md":
                from atlassian_skills.core.format.markdown import md_to_jira_wiki

                body_text = md_to_jira_wiki(
                    body_text,
                    heading_promotion=heading_promotion or "jira",
                    passthrough_prefixes=passthrough_prefix or None,
                )
            fields["description"] = body_text
        if fields_json:
            try:
                fields.update(json.loads(fields_json))
            except json.JSONDecodeError as exc:
                raise ValidationError(f"Invalid --fields-json: {exc}") from exc
        customfield_updates = _parse_customfield_updates(set_customfield)
        fields.update(customfield_updates)

        if dry_run:
            typer.echo(
                format_dry_run("PUT", f"{client.base_url}/rest/api/2/issue/{key}", body={"fields": fields})
            )
            return

        result = client.update_issue(key, fields=fields or None)
        _verify_customfield_updates(client, key, customfield_updates)
        typer.echo(format_output(result or {"status": "updated", "key": key}, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# issue delete
# ---------------------------------------------------------------------------


@issue_app.command("delete")
def issue_delete(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be sent"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Delete a Jira issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        if dry_run:
            typer.echo(format_dry_run("DELETE", f"{client.base_url}/rest/api/2/issue/{key}"))
            return
        client.delete_issue(key)
        typer.echo(format_output({"status": "deleted", "key": key}, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# issue transition
# ---------------------------------------------------------------------------


@issue_app.command("transition")
def issue_transition(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    transition_id: str = typer.Option(..., "--transition-id", help="Transition ID"),
    comment: str | None = typer.Option(None, "--comment", help="Transition comment"),
    fields_json: str | None = typer.Option(None, "--fields-json", help="Transition fields as JSON"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be sent"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Transition a Jira issue to a new status."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        try:
            extra_fields = json.loads(fields_json) if fields_json else None
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Invalid --fields-json: {exc}") from exc

        if dry_run:
            client = _make_client(ctx.obj)
            body: dict[str, Any] = {"transition": {"id": transition_id}}
            if extra_fields:
                body["fields"] = extra_fields
            if comment:
                body["update"] = {"comment": [{"add": {"body": comment}}]}
            typer.echo(
                format_dry_run("POST", f"{client.base_url}/rest/api/2/issue/{key}/transitions", body=body)
            )
            return

        client = _make_client(ctx.obj)
        client.transition_issue(key, transition_id, fields=extra_fields, comment=comment)
        typer.echo(format_output({"status": "transitioned", "key": key, "transition_id": transition_id}, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# comment list / add / edit
# ---------------------------------------------------------------------------


@comment_app.command("list")
def comment_list(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List comments on an issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        comments = client.list_comments(key)
        typer.echo(format_output(comments, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@comment_app.command("add")
def comment_add(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    body_file: str | None = typer.Option(None, "--body-file", help="Comment body file (- for stdin)"),
    body: str | None = typer.Option(None, "--body", help="Comment body text"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be sent"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Add a comment to an issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        text = read_body(body=body, body_file=body_file)
        if dry_run:
            client = _make_client(ctx.obj)
            typer.echo(
                format_dry_run("POST", f"{client.base_url}/rest/api/2/issue/{key}/comment", body={"body": text})
            )
            return
        client = _make_client(ctx.obj)
        result = client.add_comment(key, text)
        typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@comment_app.command("edit")
def comment_edit(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    comment_id: str = typer.Argument(..., help="Comment ID"),
    body_file: str | None = typer.Option(None, "--body-file", help="Comment body file (- for stdin)"),
    body: str | None = typer.Option(None, "--body", help="Comment body text"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Edit an existing comment."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        text = read_body(body=body, body_file=body_file)
        client = _make_client(ctx.obj)
        result = client.edit_comment(key, comment_id, text)
        typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@comment_app.command("delete")
def comment_delete(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    comment_id: str = typer.Argument(..., help="Comment ID"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be sent"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Delete a comment from an issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        if dry_run:
            typer.echo(
                format_dry_run(
                    "DELETE",
                    f"{client.base_url}/rest/api/2/issue/{key}/comment/{comment_id}",
                )
            )
            return
        client.delete_comment(key, comment_id)
        typer.echo(format_output({"status": "deleted", "key": key, "comment_id": comment_id}, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# worklog add
# ---------------------------------------------------------------------------


@worklog_app.command("add")
def worklog_add(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    time_spent_seconds: int = typer.Option(..., "--time-spent-seconds", help="Time spent in seconds"),
    comment: str | None = typer.Option(None, "--comment", help="Worklog comment"),
    started: str | None = typer.Option(None, "--started", help="Started datetime (ISO format)"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Add a worklog entry to an issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = client.add_worklog(key, time_spent_seconds, comment=comment, started=started)
        typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# link create / remote-create / delete
# ---------------------------------------------------------------------------


@link_app.command("create")
def link_create(
    ctx: typer.Context,
    type: str = typer.Option(..., "--type", help="Link type name"),
    inward: str = typer.Option(..., "--inward", help="Inward issue key"),
    outward: str = typer.Option(..., "--outward", help="Outward issue key"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be sent"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Create an issue link."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        if dry_run:
            client = _make_client(ctx.obj)
            body = {"type": {"name": type}, "inwardIssue": {"key": inward}, "outwardIssue": {"key": outward}}
            typer.echo(format_dry_run("POST", f"{client.base_url}/rest/api/2/issueLink", body=body))
            return
        client = _make_client(ctx.obj)
        result = client.create_issue_link(type, inward, outward)
        typer.echo(format_output(result or {"status": "linked"}, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@link_app.command("remote-create")
def link_remote_create(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    url: str = typer.Option(..., "--url", help="Remote URL"),
    title: str = typer.Option(..., "--title", help="Link title"),
    relationship: str | None = typer.Option(None, "--relationship", help="Relationship label"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Create a remote issue link."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = client.create_remote_issue_link(key, url, title, relationship=relationship)
        typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@link_app.command("delete")
def link_delete(
    ctx: typer.Context,
    link_id: str = typer.Argument(..., help="Issue link ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Delete an issue link."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        client.remove_issue_link(link_id)
        typer.echo(format_output({"status": "deleted", "link_id": link_id}, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# epic link
# ---------------------------------------------------------------------------


@epic_app.command("link")
def epic_link(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key to link"),
    epic_key: str = typer.Option(..., "--epic-key", help="Epic issue key"),
    epic_field_id: str = typer.Option(..., "--epic-field-id", help="Epic link custom field ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Link an issue to an epic via custom field."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = client.link_to_epic(key, epic_key, epic_field_id)
        typer.echo(format_output(result or {"status": "linked", "key": key, "epic": epic_key}, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# watcher add / remove
# ---------------------------------------------------------------------------


@watcher_app.command("add")
def watcher_add(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    username: str = typer.Argument(..., help="Username to add"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Add a watcher to an issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        client.add_watcher(key, username)
        typer.echo(format_output({"status": "added", "key": key, "username": username}, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@watcher_app.command("remove")
def watcher_remove(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    username: str = typer.Argument(..., help="Username to remove"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Remove a watcher from an issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        client.remove_watcher(key, username)
        typer.echo(format_output({"status": "removed", "key": key, "username": username}, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# sprint create / update / add-issues
# ---------------------------------------------------------------------------


@sprint_app.command("create")
def sprint_create(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", help="Sprint name"),
    board_id: int = typer.Option(..., "--board-id", help="Board ID"),
    start: str | None = typer.Option(None, "--start", help="Start date (ISO)"),
    end: str | None = typer.Option(None, "--end", help="End date (ISO)"),
    goal: str | None = typer.Option(None, "--goal", help="Sprint goal"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Create a new sprint."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = client.create_sprint(name, board_id, start_date=start, end_date=end, goal=goal)
        typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@sprint_app.command("update")
def sprint_update(
    ctx: typer.Context,
    sprint_id: int = typer.Argument(..., help="Sprint ID"),
    name: str | None = typer.Option(None, "--name", help="Sprint name"),
    state: str | None = typer.Option(None, "--state", help="Sprint state"),
    start: str | None = typer.Option(None, "--start", help="Start date (ISO)"),
    end: str | None = typer.Option(None, "--end", help="End date (ISO)"),
    goal: str | None = typer.Option(None, "--goal", help="Sprint goal"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Update a sprint."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = client.update_sprint(sprint_id, name=name, state=state, start_date=start, end_date=end, goal=goal)
        typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@sprint_app.command("add-issues")
def sprint_add_issues(
    ctx: typer.Context,
    sprint_id: int = typer.Argument(..., help="Sprint ID"),
    keys: list[str] = typer.Argument(..., help="Issue keys (space-separated)"),  # noqa: B008
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Add issues to a sprint."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        client.add_issues_to_sprint(sprint_id, keys)
        typer.echo(format_output({"status": "added", "sprint_id": sprint_id, "issues": keys}, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# project versions-create
# ---------------------------------------------------------------------------


@project_app.command("versions-create")
def project_versions_create(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", "-p", help="Project key"),
    name: str = typer.Option(..., "--name", help="Version name"),
    start_date: str | None = typer.Option(None, "--start-date", help="Start date"),
    release_date: str | None = typer.Option(None, "--release-date", help="Release date"),
    description: str | None = typer.Option(None, "--description", help="Version description"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Create a project version."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = client.create_version(
            project, name, start_date=start_date, release_date=release_date, description=description
        )
        typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# attachment upload / delete
# ---------------------------------------------------------------------------


@attachment_app.command("upload")
def attachment_upload(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    file: str = typer.Argument(..., help="File path to upload"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be sent"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Upload an attachment to an issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        if dry_run:
            typer.echo(
                format_dry_run(
                    "POST",
                    f"{client.base_url}/rest/api/2/issue/{key}/attachments",
                    headers={"X-Atlassian-Token": "nocheck"},
                    body=f"[multipart: {file}]",
                )
            )
            return
        result = client.upload_attachment(key, file)
        typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@attachment_app.command("delete")
def attachment_delete(
    ctx: typer.Context,
    att_id: str = typer.Argument(..., help="Attachment ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Delete an attachment."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        client.delete_attachment(att_id)
        typer.echo(format_output({"status": "deleted", "attachment_id": att_id}, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# issue-batch create
# ---------------------------------------------------------------------------


@issue_batch_app.command("create")
def issue_batch_create(
    ctx: typer.Context,
    json_file: str = typer.Option(..., "--json-file", help="JSON file with list of issue field dicts"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be sent"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Batch create issues from a JSON file."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        from pathlib import Path

        try:
            data = json.loads(Path(json_file).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Invalid JSON file: {exc}") from exc
        if not isinstance(data, list):
            typer.echo("Error: JSON file must contain a list of issue field objects", err=True)
            raise typer.Exit(7)

        if dry_run:
            client = _make_client(ctx.obj)
            typer.echo(
                format_dry_run(
                    "POST",
                    f"{client.base_url}/rest/api/2/issue/bulk",
                    body={"issueUpdates": [{"fields": i} for i in data]},
                )
            )
            return

        client = _make_client(ctx.obj)
        result = client.batch_create_issues(data)
        typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)
