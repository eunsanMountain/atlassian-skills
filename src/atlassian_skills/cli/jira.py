from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import typer

from atlassian_skills.core.auth import resolve_credential
from atlassian_skills.core.config import get_profile, load_config
from atlassian_skills.core.dryrun import format_dry_run
from atlassian_skills.core.errors import AtlasError, ValidationError, request_context_line
from atlassian_skills.core.format import OutputFormat, format_output
from atlassian_skills.core.format.markdown import (
    JiraMarkdownResult,
    WriteConversionResult,
    _SectionNotFoundError,
    format_md_issue,
    jira_wiki_to_md_result,
    jira_wiki_to_md_with_options_result,
    md_to_jira_wiki_result,
)
from atlassian_skills.core.models import WriteResult
from atlassian_skills.core.stdin import read_body
from atlassian_skills.core.tls import build_ssl_context
from atlassian_skills.jira import description_md, description_merge, description_push, description_wiki
from atlassian_skills.jira.client import JiraClient
from atlassian_skills.jira.models import Issue, IssueDates, JiraAttachment
from atlassian_skills.jira.read_projection import (
    REQUESTED_PROJECTION,
    JiraReadReport,
    assess_jira_read,
)

jira_app = typer.Typer(help="Jira commands", no_args_is_help=True)

# Sub-groups
user_app = typer.Typer(help="User commands", no_args_is_help=True)
issue_app = typer.Typer(help="Issue commands", no_args_is_help=True)
#: Hidden until the whole managed contract lands. A description workflow that
#: can pull and push but cannot merge or recover is worse than none: it invites
#: a caller to depend on a half of it that has no safe answer for the other
#: half. `wiki` works today and is exposed with the rest, not before it.
description_app = typer.Typer(help="Managed description workflows", no_args_is_help=True)
description_wiki_app = typer.Typer(help="Exact Jira wiki workflow: pull, edit, compare, push", no_args_is_help=True)
description_md_app = typer.Typer(help="Managed Markdown workflow: pull, validate, compare, push", no_args_is_help=True)
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
issue_app.add_typer(description_app, name="description")
description_app.add_typer(description_wiki_app, name="wiki")
description_app.add_typer(description_md_app, name="md")
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
    return JiraClient(
        url.rstrip("/"),
        credential,
        timeout=timeout,
        verify=build_ssl_context(profile.ca_bundle),
        verbose=int(ctx_obj.get("verbose", 0)),
    )


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
        request_line = request_context_line(err)
        if request_line:
            typer.echo(request_line, err=True)
        if err.hint:
            typer.echo(f"Hint:  {err.hint}", err=True)
    raise typer.Exit(err.exit_code)


def _emit_conversion_warnings(ctx: typer.Context, warnings: tuple[str, ...]) -> None:
    if ctx.obj.get("quiet"):
        return
    for message in warnings:
        typer.echo(f"# conversion: {message}", err=True)


def _conversion_diagnostics(result: WriteConversionResult) -> dict[str, Any]:
    return {
        "push_safe": result.push_safe,
        "warnings": list(result.warnings),
        "losses": list(result.losses),
    }


def _assert_write_conversion_safe(result: WriteConversionResult) -> None:
    if result.push_safe and not result.losses:
        return
    raise ValidationError(
        "The Markdown body cannot be converted without losing content.",
        hint="Resolve the reported conversion losses before publishing.",
        context=_conversion_diagnostics(result),
    )


#: What `--body-format` accepts on a write. `wiki` is the default and means the
#: text is already Jira markup.
BODY_FORMATS = ("md", "wiki")
#: What `--body-repr` accepts on a read. `raw` and `wiki` both mean the stored
#: markup, unconverted.
BODY_REPRS = ("md", "raw", "wiki")


def _checked_choice(value: str | None, allowed: tuple[str, ...], option: str) -> str | None:
    """Refuse a value this command does not understand, rather than ignoring it.

    Every one of these options was compared against a single literal, so anything
    else fell through to the default branch. `--body-format markdown` is a
    plausible typo and it did not fail: it published the Markdown to Jira as
    though it were already wiki markup, which is a wrong issue body and no error
    anywhere.

    The refusal names what was passed and what is accepted, because a caller who
    has just been told "invalid" and not "invalid, try one of these" guesses
    again.
    """

    if value is None or value in allowed:
        return value
    raise ValidationError(
        f"{option} does not accept {value!r}",
        hint=f"use one of: {', '.join(allowed)}",
        context={"reason": "unknown_body_option", "option": option, "value": value, "allowed": list(allowed)},
    )


def _merge_extra_fields(fields: dict[str, Any], fields_json: str | None) -> None:
    """Add the caller's extra fields, and refuse to let them replace a converted one.

    `--fields-json` used to be applied last and win. Combined with `--body-format
    md` that meant the body was read, converted, checked for conversion losses --
    and then thrown away:

        --body-format md --body-file b.md --fields-json '{"description": "..."}'
        PUT {"fields": {"description": "..."}}

    The Markdown never reached the server and nothing said so. Every safety
    check on the write path had run against a body that was not published.

    Refused rather than resolved in either direction. Preferring the converted
    body would ignore what the caller typed; preferring the JSON is what already
    happens and is the bug. The two instructions genuinely conflict, and the
    caller is the only one who knows which they meant.
    """

    if not fields_json:
        return
    try:
        extra = json.loads(fields_json)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid --fields-json: {exc}") from exc
    if not isinstance(extra, dict):
        raise ValidationError(
            "--fields-json must be a JSON object of field names to values",
            context={"reason": "fields_json_not_an_object", "type": type(extra).__name__},
        )
    clashes = sorted(name for name in extra if name in fields)
    if clashes:
        raise ValidationError(
            f"--fields-json would replace {', '.join(clashes)}, which this command already set",
            hint="pass the value one way or the other, not both",
            context={"reason": "fields_json_conflict", "fields": clashes},
        )
    fields.update(extra)


def _write_readback(client: JiraClient, key: str, sent_description: str | None) -> dict[str, Any]:
    """Read the issue back after a write, and say what the server actually kept.

    Two things a caller could not get before, both of them cheap -- one GET of
    two fields.

    `updated` is what the next write needs for `--if-updated`. Without it a
    caller has to guess or re-read, and a caller who guesses stops passing the
    flag. Measured on the sandbox: the field carries milliseconds and three
    writes in a row produced three distinct values, so it is fine to compare
    exactly.

    `description_matches_sent` is the check that was missing. Confluence rewrites
    what it stores; the same probe found Jira storing bodies byte for byte, which
    is a reason to expect a match and not a reason to skip looking.
    """

    readback: dict[str, Any] = {}
    try:
        raw = client.get_issue_raw_text(key, fields=["updated", "description"])
        fields = (json.loads(raw).get("fields") or {}) if raw else {}
    except Exception:  # noqa: BLE001 - the write succeeded; failing to describe it must not fail the command
        return {"readback": "unavailable"}
    updated = fields.get("updated")
    if isinstance(updated, str):
        readback["updated"] = updated
    if sent_description is not None:
        stored = fields.get("description") or ""
        readback["description_matches_sent"] = stored == sent_description
        if stored != sent_description:
            readback["stored_description"] = stored
    return readback


def _read_conversion_payload(
    conversion: JiraMarkdownResult | None,
    flattened: tuple[str, ...],
) -> dict[str, Any]:
    """What the conversion found, not only the sentences it produced.

    `warnings` keeps its old shape and meaning so that anything already reading
    this payload is unaffected. The rest is what atls used to compute and throw
    away at the boundary.

    `attachments` matters most. The Markdown renders `![](design.png)` whether or
    not that file is anywhere the caller can reach, so listing the filenames is
    what lets a caller notice that it has a reference and not a picture.
    """

    payload: dict[str, Any] = {"warnings": list(flattened)}
    if conversion is None:
        return payload
    payload["losses"] = list(conversion.losses)
    payload["attachments"] = list(conversion.attachments)
    payload["push_safe"] = conversion.push_safe
    return payload


def _read_projection_payload(
    issue_key: str,
    source_wiki: str,
    markdown: str,
    conversion: JiraMarkdownResult | None,
    *,
    requested_projection: bool,
) -> dict[str, Any]:
    """Whether this Markdown still says what the issue says."""

    report = assess_jira_read(
        source_wiki,
        markdown,
        document=conversion.document if conversion else None,
        losses=conversion.losses if conversion else (),
        attachments=conversion.attachments if conversion else (),
        requested_projection=requested_projection,
    )
    return report.to_dict(issue_key)


def _emit_read_projection_warning(ctx: typer.Context, report: JiraReadReport, issue_key: str) -> None:
    """Say what is actually wrong with this body, and only that.

    Three different problems and three different things to do about them. Saying
    "do not summarize this" about a body whose every word is present is how a
    warning stops being read -- and on the measured corpus that is most of them:
    the text always survives, and what varies is whether writing it back would
    change the issue.

    Silent when there is nothing to say, which is most bodies.
    """

    if ctx.obj.get("quiet") or not report.attention_required:
        return
    if report.reason == REQUESTED_PROJECTION:
        typer.echo("INFO     this is the section you asked for, not the whole body", err=True)
    elif not report.content_complete:
        typer.echo("WARNING  this Markdown is missing part of the issue body (content_incomplete)", err=True)
        typer.echo("         do not summarize this issue from this output alone", err=True)
    else:
        # The words are all here. Reading it is fine; publishing it is not.
        typer.echo("WARNING  writing this Markdown back would change the issue (body_would_change)", err=True)
        typer.echo("         reading it is fine -- the text is all here", err=True)
        if report.first_difference is not None:
            stored, would_write = report.first_difference
            typer.echo(f"         stored:     {stored}", err=True)
            typer.echo(f"         would save: {would_write}", err=True)
    for loss in report.losses:
        typer.echo(f"         {loss}", err=True)
    for action in report.to_dict(issue_key).get("next_actions", []):
        typer.echo(f"         next: atls {' '.join(action['argv'])}", err=True)
        break


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
        _emit_conversion_warnings(ctx, conversion.warnings + conversion.losses)
        return output
    if isinstance(output, dict):
        payload: Any = dict(output)
    elif hasattr(output, "model_dump"):
        payload = output.model_dump(mode="json", exclude_none=True)
    else:
        payload = output
    if isinstance(payload, dict):
        payload["conversion"] = _conversion_diagnostics(conversion)
        return payload
    return {"result": payload, "conversion": _conversion_diagnostics(conversion)}


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
        typer.echo(format_output(user, fmt))
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
        user = client.get_myself()
        typer.echo(format_output(user, fmt))
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
        body_repr = _checked_choice(body_repr, BODY_REPRS, "--body-repr")
        client = _make_client(ctx.obj)

        # RAW format: return server response text verbatim (byte-preserving contract)
        if fmt == OutputFormat.RAW:
            typer.echo(client.get_issue_raw_text(key, fields=field_list))
            return

        issue = client.get_issue(key, fields=field_list)
        conversion_warnings: tuple[str, ...] = ()
        #: The conversion itself, kept so the JSON path can report what it found
        #: rather than only the sentences it produced.
        read_conversion: JiraMarkdownResult | None = None
        #: The stored markup, kept because the conversion overwrites it and the
        #: completeness check needs something to compare the Markdown against.
        source_wiki = issue.description or ""

        # Task 1: --body-repr conversion on the description field.
        if body_repr and issue.description:
            if body_repr == "md":
                conversion = jira_wiki_to_md_result(issue.description)
                issue.description = conversion.markdown
                conversion_warnings += conversion.all_warnings
                read_conversion = conversion
            # "raw" and "wiki" keep the original wiki markup (Server stores wiki natively)

        if fmt == OutputFormat.MD and (section or heading_promotion or notice_prefixes):
            description_raw = issue.description or ""
            if not body_repr or body_repr == "md":
                # body_repr=md: already converted, but section/notice extraction still works on md text
                # body_repr unset: full wiki→md conversion + section extraction
                try:
                    conversion = jira_wiki_to_md_with_options_result(
                        description_raw,
                        section=section,
                        heading_promotion=heading_promotion,
                        drop_leading_notice=notice_prefixes,
                        skip_conversion=bool(body_repr),
                    )
                    body_md = conversion.markdown
                    conversion_warnings += conversion.all_warnings
                    read_conversion = conversion
                except _SectionNotFoundError as exc:
                    raise ValidationError(f"Section '{exc.section}' not found in issue body") from exc
            else:
                # body_repr=raw/wiki: keep as-is, section extraction not meaningful on wiki markup
                body_md = description_raw
            typer.echo(body_md)
            _emit_conversion_warnings(ctx, conversion_warnings)
        elif fmt == OutputFormat.MD and body_repr:
            # body_repr already set the body representation → skip format_md_issue's wiki→md conversion
            data = issue.model_dump()
            typer.echo(format_md_issue(data, skip_body_conversion=True))
            _emit_conversion_warnings(ctx, conversion_warnings)
        elif fmt == OutputFormat.MD:
            conversion = jira_wiki_to_md_result(issue.description or "")
            issue.description = conversion.markdown
            read_conversion = conversion
            typer.echo(format_md_issue(_issue_to_json_dict(issue), skip_body_conversion=True))
            _emit_conversion_warnings(ctx, conversion.all_warnings)
        elif fmt == OutputFormat.JSON and body_repr == "md":
            payload = _issue_to_json_dict(issue)
            payload["conversion"] = _read_conversion_payload(read_conversion, conversion_warnings)
            payload.update(
                _read_projection_payload(
                    key,
                    source_wiki,
                    issue.description or "",
                    read_conversion,
                    requested_projection=bool(section or notice_prefixes),
                )
            )
            typer.echo(format_output(payload, fmt))
        else:
            typer.echo(_render_issue(issue, fmt))
        # Every path that handed back converted Markdown, including the plain
        # `--body-repr=md` one whose whole output is the body. The JSON path
        # carries the same verdict in the payload and says nothing here, so a
        # caller parsing stdout is not handed a second copy on stderr.
        if fmt != OutputFormat.JSON and read_conversion is not None:
            _emit_read_projection_warning(
                ctx,
                assess_jira_read(
                    source_wiki,
                    issue.description or "",
                    document=read_conversion.document,
                    losses=read_conversion.losses,
                    attachments=read_conversion.attachments,
                    requested_projection=bool(section or notice_prefixes),
                ),
                key,
            )
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
            conversion_warnings: list[str] = []
            for issue in result.issues:
                description_raw = issue.description or ""
                try:
                    conversion = jira_wiki_to_md_with_options_result(
                        description_raw,
                        section=section,
                        heading_promotion=heading_promotion,
                        drop_leading_notice=notice_prefixes,
                    )
                    body_md = conversion.markdown
                    conversion_warnings.extend(f"{issue.key}: {message}" for message in conversion.all_warnings)
                except _SectionNotFoundError as exc:
                    raise ValidationError(f"Section '{exc.section}' not found in issue '{issue.key}' body") from exc
                parts.append(f"# {issue.key}: {issue.summary or ''}\n\n{body_md}")
            typer.echo("\n\n---\n\n".join(parts))
            _emit_conversion_warnings(ctx, tuple(conversion_warnings))
        elif fmt == OutputFormat.MD:
            parts = []
            conversion_warnings = []
            for issue in result.issues:
                conversion = jira_wiki_to_md_result(issue.description or "")
                issue.description = conversion.markdown
                parts.append(format_md_issue(_issue_to_json_dict(issue), skip_body_conversion=True))
                conversion_warnings.extend(f"{issue.key}: {message}" for message in conversion.all_warnings)
            typer.echo("\n\n---\n\n".join(parts))
            _emit_conversion_warnings(ctx, tuple(conversion_warnings))
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
        typer.echo(format_output(transitions, fmt))
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
        typer.echo(format_output(IssueDates.model_validate(dates), fmt))
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
        raw_images = client.get_issue_images(key)
        images = [JiraAttachment.model_validate(a) for a in raw_images]
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
        typer.echo(format_output(fields, fmt))
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
        typer.echo(format_output(projects, fmt))
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
        typer.echo(format_output(versions, fmt))
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
        typer.echo(format_output(components, fmt))
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
        typer.echo(format_output(link_types, fmt))
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
        typer.echo(format_output(wl, fmt))
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
        typer.echo(format_output(wl, fmt))
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
    """Download attachments for an issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        paths = client.download_attachments(key, output_dir)
        typer.echo(format_output([{"downloaded": str(path)} for path in paths], fmt), err=False)
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
    body_format: str | None = typer.Option(None, "--body-format", help="Body format: md|wiki (default wiki)"),
    fields_json: str | None = typer.Option(None, "--fields-json", help="Extra fields as JSON string"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be sent"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Create a new Jira issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        body_format = _checked_choice(body_format, BODY_FORMATS, "--body-format")
        fields: dict[str, Any] = {
            "project": {"key": project},
            "issuetype": {"name": type},
            "summary": summary,
        }
        body_conversion = WriteConversionResult(body="")
        if body_file:
            body_text = read_body(body_file=body_file)
            if body_format == "md":
                body_conversion = md_to_jira_wiki_result(body_text)
                _assert_write_conversion_safe(body_conversion)
                body_text = body_conversion.body
            fields["description"] = body_text
        _merge_extra_fields(fields, fields_json)

        if dry_run:
            client = _make_client(ctx.obj)
            _emit_conversion_warnings(ctx, body_conversion.warnings + body_conversion.losses)
            typer.echo(format_dry_run("POST", f"{client.base_url}/rest/api/2/issue", body={"fields": fields}))
            return

        client = _make_client(ctx.obj)
        result = client.create_issue(fields)
        if fmt == OutputFormat.COMPACT:
            output = WriteResult(action="created", key=result.get("key", ""), summary=summary)
            typer.echo(format_output(_output_with_write_diagnostics(ctx, output, fmt, body_conversion), fmt))
        else:
            typer.echo(format_output(_output_with_write_diagnostics(ctx, result, fmt, body_conversion), fmt))
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
    body_format: str | None = typer.Option(None, "--body-format", help="Body format: md|wiki (default wiki)"),
    fields_json: str | None = typer.Option(None, "--fields-json", help="Fields as JSON string"),
    set_customfield: list[str] | None = typer.Option(
        None,
        "--set-customfield",
        help="KEY=VAL custom field (read-back verified; use --fields-json for structured values)",
    ),  # noqa: B008
    if_updated: str | None = typer.Option(None, "--if-updated", help="ISO8601 timestamp for stale check"),
    heading_promotion: str | None = typer.Option(
        None, "--heading-promotion", help="Heading promotion: jira|confluence|none"
    ),
    passthrough_prefix: list[str] = typer.Option(
        [], "--passthrough-prefix", help="Passthrough prefixes for md→wiki conversion only"
    ),  # noqa: B008
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be sent"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Update an existing Jira issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        body_format = _checked_choice(body_format, BODY_FORMATS, "--body-format")
        client = _make_client(ctx.obj)

        # Stale check: compare --if-updated with the issue's updated field
        if if_updated is not None:
            from atlassian_skills.core.errors import StaleError

            issue = client.get_issue(key)
            server_updated = issue.updated or ""
            if server_updated != if_updated:
                raise StaleError(
                    f"Stale issue: expected updated={if_updated}, server has {server_updated}",
                    context={"server_updated": server_updated, "expected_updated": if_updated},
                )

        fields: dict[str, Any] = {}
        body_conversion = WriteConversionResult(body="")
        if body_file:
            body_text = read_body(body_file=body_file)
            if body_format == "md":
                body_conversion = md_to_jira_wiki_result(
                    body_text,
                    heading_promotion=heading_promotion or "jira",
                    passthrough_prefixes=passthrough_prefix or None,
                )
                _assert_write_conversion_safe(body_conversion)
                body_text = body_conversion.body
            fields["description"] = body_text
        _merge_extra_fields(fields, fields_json)
        customfield_updates = _parse_customfield_updates(set_customfield)
        fields.update(customfield_updates)

        if dry_run:
            _emit_conversion_warnings(ctx, body_conversion.warnings + body_conversion.losses)
            typer.echo(format_dry_run("PUT", f"{client.base_url}/rest/api/2/issue/{key}", body={"fields": fields}))
            return

        result = client.update_issue(key, fields=fields or None)
        _verify_customfield_updates(client, key, customfield_updates)
        readback = _write_readback(client, key, fields.get("description"))
        if fmt == OutputFormat.COMPACT:
            compact_output = WriteResult(action="updated", key=key)
            typer.echo(format_output(_output_with_write_diagnostics(ctx, compact_output, fmt, body_conversion), fmt))
        else:
            result_output = {**(result or {"status": "updated", "key": key}), **readback}
            typer.echo(format_output(_output_with_write_diagnostics(ctx, result_output, fmt, body_conversion), fmt))
        if readback.get("description_matches_sent") is False:
            typer.echo("WARNING  the server stored a different body than was sent", err=True)
            typer.echo(f"         next: atls jira issue get {key} --fields description --format=raw", err=True)
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
        if fmt == OutputFormat.COMPACT:
            typer.echo(format_output(WriteResult(action="deleted", key=key), fmt))
        else:
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
    transition_id: str | None = typer.Option(None, "--transition-id", help="Transition ID"),
    transition_name: str | None = typer.Option(
        None, "--transition-name", help="Transition name (alternative to --transition-id)"
    ),
    comment: str | None = typer.Option(None, "--comment", help="Transition comment"),
    comment_format: str | None = typer.Option(
        None, "--comment-format", help="Comment format: md|wiki (default wiki; md converts to Jira wiki)"
    ),
    fields_json: str | None = typer.Option(None, "--fields-json", help="Transition fields as JSON"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be sent"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Transition a Jira issue to a new status."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        # `worklog add` has taken `--comment-format md` since it was written and
        # this took the comment raw, so the same Markdown published as a comment
        # or as a worklog note produced two different things on the issue. There
        # is no reason for the difference; it is where the two were written.
        comment_format = _checked_choice(comment_format, BODY_FORMATS, "--comment-format")
        comment_conversion = WriteConversionResult(body=comment or "")
        if comment and comment_format == "md":
            comment_conversion = md_to_jira_wiki_result(comment)
            _assert_write_conversion_safe(comment_conversion)
            comment = comment_conversion.body

        if not transition_id and not transition_name:
            raise ValidationError("Either --transition-id or --transition-name is required")
        if transition_id and transition_name:
            raise ValidationError("Use either --transition-id or --transition-name, not both")

        # One place decides what --fields-json means, including "it has to be an
        # object". Transition sets no fields of its own, so nothing can clash.
        transition_fields: dict[str, Any] = {}
        _merge_extra_fields(transition_fields, fields_json)
        extra_fields = transition_fields or None

        client = _make_client(ctx.obj)

        if transition_name:
            transitions = client.get_transitions(key)
            match = [t for t in transitions if t.name.lower() == transition_name.lower()]
            if not match:
                avail = "\n".join(f"  {t.id} | {t.name}" for t in transitions)
                raise ValidationError(f"No transition '{transition_name}'.\nAvailable:\n{avail}")
            transition_id = str(match[0].id)

        if dry_run:
            body: dict[str, Any] = {"transition": {"id": transition_id}}
            if extra_fields:
                body["fields"] = extra_fields
            if comment:
                body["update"] = {"comment": [{"add": {"body": comment}}]}
            _emit_conversion_warnings(ctx, comment_conversion.warnings + comment_conversion.losses)
            typer.echo(format_dry_run("POST", f"{client.base_url}/rest/api/2/issue/{key}/transitions", body=body))
            return

        if transition_id is None:
            raise typer.BadParameter("Either --transition-id or --transition-name is required")
        client.transition_issue(key, transition_id, fields=extra_fields, comment=comment)
        _emit_conversion_warnings(ctx, comment_conversion.warnings + comment_conversion.losses)
        if fmt == OutputFormat.COMPACT:
            typer.echo(format_output(WriteResult(action="transitioned", key=key), fmt))
        else:
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
    body_format: str | None = typer.Option(
        None, "--body-format", help="Body format: md|wiki (default wiki; md converts to Jira wiki)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be sent"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Add a comment to an issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        body_format = _checked_choice(body_format, BODY_FORMATS, "--body-format")
        text = read_body(body=body, body_file=body_file)
        body_conversion = WriteConversionResult(body=text)
        if body_format == "md":
            body_conversion = md_to_jira_wiki_result(text)
            _assert_write_conversion_safe(body_conversion)
            text = body_conversion.body
        if dry_run:
            client = _make_client(ctx.obj)
            _emit_conversion_warnings(ctx, body_conversion.warnings + body_conversion.losses)
            typer.echo(format_dry_run("POST", f"{client.base_url}/rest/api/2/issue/{key}/comment", body={"body": text}))
            return
        client = _make_client(ctx.obj)
        result = client.add_comment(key, text)
        if fmt == OutputFormat.COMPACT:
            output = WriteResult(action="commented", key=key, id=result.get("id"))
            typer.echo(format_output(_output_with_write_diagnostics(ctx, output, fmt, body_conversion), fmt))
        else:
            typer.echo(format_output(_output_with_write_diagnostics(ctx, result, fmt, body_conversion), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@comment_app.command("edit")
def comment_edit(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    comment_id: str = typer.Argument(..., help="Comment ID"),
    body_file: str | None = typer.Option(None, "--body-file", help="Comment body file (- for stdin)"),
    body: str | None = typer.Option(None, "--body", help="Comment body text"),
    body_format: str | None = typer.Option(
        None, "--body-format", help="Body format: md|wiki (default wiki; md converts to Jira wiki)"
    ),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Edit an existing comment."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        body_format = _checked_choice(body_format, BODY_FORMATS, "--body-format")
        text = read_body(body=body, body_file=body_file)
        body_conversion = WriteConversionResult(body=text)
        if body_format == "md":
            body_conversion = md_to_jira_wiki_result(text)
            _assert_write_conversion_safe(body_conversion)
            text = body_conversion.body
        client = _make_client(ctx.obj)
        result = client.edit_comment(key, comment_id, text)
        if fmt == OutputFormat.COMPACT:
            output = WriteResult(action="edited", key=key, summary=comment_id)
            typer.echo(format_output(_output_with_write_diagnostics(ctx, output, fmt, body_conversion), fmt))
        else:
            typer.echo(format_output(_output_with_write_diagnostics(ctx, result, fmt, body_conversion), fmt))
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
    comment_format: str | None = typer.Option(
        None, "--comment-format", help="Comment format: md (convert to Jira wiki) or wiki (raw, default)"
    ),
    started: str | None = typer.Option(None, "--started", help="Started datetime (ISO format)"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Add a worklog entry to an issue."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        # Validated here like every other body-bearing command. Without it an
        # unknown value was not an error -- it simply was not "md", so the
        # comment went out as raw Jira wiki. Measured: `--comment-format
        # markdown` reached the server and posted.
        comment_format = _checked_choice(comment_format, BODY_FORMATS, "--comment-format")
        body_conversion = WriteConversionResult(body=comment or "")
        if comment and comment_format == "md":
            body_conversion = md_to_jira_wiki_result(comment)
            _assert_write_conversion_safe(body_conversion)
            comment = body_conversion.body
        client = _make_client(ctx.obj)
        result = client.add_worklog(key, time_spent_seconds, comment=comment, started=started)
        if fmt == OutputFormat.COMPACT:
            output = WriteResult(action="worklog added", key=key, summary=str(result.get("id") or ""))
            typer.echo(format_output(_output_with_write_diagnostics(ctx, output, fmt, body_conversion), fmt))
        else:
            typer.echo(format_output(_output_with_write_diagnostics(ctx, result, fmt, body_conversion), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# link create / remote-list / remote-create / delete
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


@link_app.command("remote-list")
def link_remote_list(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List remote issue links."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = client.list_remote_issue_links(key)
        typer.echo(format_output(result, fmt))
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
        if fmt == OutputFormat.COMPACT:
            link_id = (result or {}).get("id")
            typer.echo(
                format_output(
                    WriteResult(action="remote-linked", key=key, summary=str(link_id) if link_id else None), fmt
                )
            )
        else:
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
        if fmt == OutputFormat.COMPACT:
            sprint_key = str((result or {}).get("id", ""))
            typer.echo(format_output(WriteResult(action="created", key=sprint_key, summary=name), fmt))
        else:
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
        if fmt == OutputFormat.COMPACT:
            typer.echo(format_output(WriteResult(action="updated", key=str(sprint_id)), fmt))
        else:
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
        if fmt == OutputFormat.COMPACT:
            version_key = str((result or {}).get("id", ""))
            typer.echo(format_output(WriteResult(action="created", key=version_key, summary=name), fmt))
        else:
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
        if fmt == OutputFormat.COMPACT:
            att_id = None
            if isinstance(result, list) and result:
                att_id = str(result[0].get("id")) if isinstance(result[0], dict) else None
            elif isinstance(result, dict):
                att_id = str(result.get("id")) if result.get("id") else None
            typer.echo(format_output(WriteResult(action="uploaded", key=key, id=att_id, summary=file), fmt))
        else:
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
        if fmt == OutputFormat.COMPACT:
            created = [
                WriteResult(action="created", key=str(item.get("key", "")), id=str(item.get("id", "")) or None)
                for item in (result or {}).get("issues", [])
                if isinstance(item, dict)
            ]
            typer.echo(format_output(created, fmt))
        else:
            typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# --------------------------------------------------------------------------
# Exact Jira wiki description workflow
# --------------------------------------------------------------------------


@description_wiki_app.command("pull")
def description_wiki_pull(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    output: str = typer.Option(..., "--output", "-o", help="Where to write the description"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Write the description exactly as Jira stores it, with its binding."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = description_wiki.pull_wiki(client, key, output_path=Path(output), site=str(client.base_url))
        typer.echo(format_output(result, fmt))
    except AtlasError as error:
        _handle_error(error, fmt)


@description_wiki_app.command("validate")
def description_wiki_validate(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Description file"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Offline: what this file can still do, and what it cannot."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        typer.echo(format_output(description_wiki.validate_wiki(Path(path)), fmt))
    except AtlasError as error:
        _handle_error(error, fmt)


# `compare`, the same verb Confluence uses for the same question. Across this CLI `diff`
# means two things that already exist on the server -- `confluence page diff` between two
# versions, `bitbucket pr diff` -- and a local file is not one of those. New in 0.4.0 and
# never published as `diff`, so there is no alias to keep.
@description_wiki_app.command("compare")
def description_wiki_diff(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    path: str = typer.Argument(..., help="Description file"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """What a push would change, against the description as it is now."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        typer.echo(format_output(description_wiki.diff_wiki(client, key, Path(path)), fmt))
    except AtlasError as error:
        _handle_error(error, fmt)


@description_wiki_app.command("push")
def description_wiki_push(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    wiki_file: str = typer.Option(..., "--wiki-file", help="Description file to publish"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be written without writing"),
    allow_unbound: bool = typer.Option(
        False, "--allow-unbound", help="Publish a file with no binding, without a stale check"
    ),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Publish the file as the description, refusing if the issue moved."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = description_wiki.push_wiki(client, key, Path(wiki_file), dry_run=dry_run, allow_unbound=allow_unbound)
        typer.echo(format_output(result, fmt))
    except AtlasError as error:
        _handle_error(error, fmt)


@description_md_app.command("pull")
def description_md_pull(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    output: str = typer.Option(..., "--output", "-o", help="Where to write the Markdown"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Write the description as Markdown, if it could be published back."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = description_md.pull_md(client, key, output_path=Path(output), site=str(client.base_url))
        typer.echo(format_output(result, fmt))
    except AtlasError as error:
        _handle_error(error, fmt)


@description_md_app.command("validate")
def description_md_validate(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Managed Markdown file"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Offline: is this file still a managed Markdown description?"""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        typer.echo(format_output(description_md.validate_md(Path(path)), fmt))
    except AtlasError as error:
        _handle_error(error, fmt)


@description_md_app.command("compare")
def description_md_diff(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    path: str = typer.Argument(..., help="Managed Markdown file"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """What changed locally, and what changed on the server, kept apart."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        typer.echo(format_output(description_md.diff_md(client, key, Path(path)), fmt))
    except AtlasError as error:
        _handle_error(error, fmt)


@description_md_app.command("push")
def description_md_push(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    md_file: str = typer.Option(..., "--md-file", help="Managed Markdown file to publish"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Prove the candidate and report without writing"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Publish the Markdown as the description, proving it carries the identity."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = description_push.push_md(client, key, Path(md_file), dry_run=dry_run)
        typer.echo(format_output(result, fmt))
    except AtlasError as error:
        _handle_error(error, fmt)


# --------------------------------------------------------------------------
# Merge and authority: shared by both representations, so directly under
# `description` rather than under one of them.
# --------------------------------------------------------------------------


# The names Confluence made public in §7.1, so one workflow has one vocabulary.
#
# D3. Confluence demoted `prepare-merge`/`finalize-merge` to hidden aliases and made
# `prepare-reconcile`/`record-reconciled-against` the public pair; Jira kept the old names,
# so the same operation on the same kind of document had two names depending on which
# product you had reached. An agent reads one skill for both.
#
# The old names stay as hidden aliases rather than disappearing: they are in scripts, and a
# rename that breaks a caller to tidy a help listing is a worse trade than a hidden alias.
@description_app.command("prepare-reconcile")
@description_app.command("prepare-merge", hidden=True)
def description_prepare_merge(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    file: str = typer.Option(..., "--file", help="Description file whose baseline moved"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Lay out base, local and remote as three files. Merges nothing."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        typer.echo(format_output(description_merge.prepare_merge(client, key, Path(file)), fmt))
    except AtlasError as error:
        _handle_error(error, fmt)


@description_app.command("record-reconciled-against")
@description_app.command("finalize-merge", hidden=True)
def description_finalize_merge(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    file: str = typer.Option(..., "--file", help="Description file the merge belongs to"),
    merged: str = typer.Option(..., "--merged", help="The merged text a person wrote"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Re-bind the merged text, refusing if the issue moved again meanwhile."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = description_merge.finalize_merge(client, key, Path(file), merged=Path(merged))
        typer.echo(format_output(result, fmt))
    except AtlasError as error:
        _handle_error(error, fmt)


@description_app.command("set-authority")
def description_set_authority(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Issue key"),
    file: str = typer.Option(..., "--file", help="Description file to hand authority to"),
    to: str = typer.Option(..., "--to", help="Which representation publishes from here: md|wiki"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Move which representation publishes, re-reading the issue while doing it."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        typer.echo(format_output(description_merge.set_authority(client, key, Path(file), to=to), fmt))
    except AtlasError as error:
        _handle_error(error, fmt)
