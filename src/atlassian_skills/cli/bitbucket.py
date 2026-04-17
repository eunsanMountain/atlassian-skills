from __future__ import annotations

import json
import os
from typing import Any

import typer

from atlassian_skills.bitbucket.client import BitbucketClient
from atlassian_skills.core.auth import resolve_credential
from atlassian_skills.core.config import get_profile, load_config
from atlassian_skills.core.dryrun import format_dry_run
from atlassian_skills.core.errors import AtlasError
from atlassian_skills.core.format import OutputFormat, format_output
from atlassian_skills.core.models import WriteResult
from atlassian_skills.core.stdin import read_body

bitbucket_app = typer.Typer(help="Bitbucket commands", no_args_is_help=True)

# Sub-groups
project_app = typer.Typer(help="Project commands", no_args_is_help=True)
repo_app = typer.Typer(help="Repository commands", no_args_is_help=True)
pr_app = typer.Typer(help="Pull request commands", no_args_is_help=True)
branch_app = typer.Typer(help="Branch commands", no_args_is_help=True)
file_app = typer.Typer(help="File commands", no_args_is_help=True)
comment_app = typer.Typer(help="PR comment commands", no_args_is_help=True)
task_app = typer.Typer(help="PR task commands", no_args_is_help=True)

bitbucket_app.add_typer(project_app, name="project")
bitbucket_app.add_typer(repo_app, name="repo")
bitbucket_app.add_typer(pr_app, name="pr")
bitbucket_app.add_typer(branch_app, name="branch")
bitbucket_app.add_typer(file_app, name="file")
bitbucket_app.add_typer(comment_app, name="comment")
bitbucket_app.add_typer(task_app, name="task")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(ctx_obj: dict[str, Any]) -> BitbucketClient:
    profile_name: str = ctx_obj.get("profile", "default")
    timeout: float = ctx_obj.get("timeout", 30.0)
    config = load_config()
    profile = get_profile(config, profile_name)
    url = profile.bitbucket_url or os.environ.get(f"ATLS_{profile_name.upper()}_BITBUCKET_URL")
    if not url:
        typer.echo(
            f"No Bitbucket URL for profile '{profile_name}'. "
            f"Set bitbucket_url in config or ATLS_{profile_name.upper()}_BITBUCKET_URL env var.",
            err=True,
        )
        raise typer.Exit(1)
    credential = resolve_credential(profile_name, "bitbucket", profile)
    verify: str | bool = profile.ca_bundle if profile.ca_bundle else True
    return BitbucketClient(url.rstrip("/"), credential, timeout=timeout, verify=verify)


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


def _diff_file_fmt(fmt: OutputFormat) -> OutputFormat:
    """For diff/file commands, md is treated as raw."""
    return OutputFormat.RAW if fmt == OutputFormat.MD else fmt


# ---------------------------------------------------------------------------
# project list
# ---------------------------------------------------------------------------


@project_app.command("list")
def project_list(
    ctx: typer.Context,
    name: str | None = typer.Option(None, "--name", help="Filter by project name"),
    limit: int = typer.Option(25, "--limit", help="Results per page"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """List Bitbucket projects."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        projects = client.list_projects(name=name, limit=limit)

        if fmt == OutputFormat.JSON or fmt == OutputFormat.RAW:
            typer.echo(format_output([p.model_dump() for p in projects], fmt))
        else:
            for p in projects:
                typer.echo(f"{p.key:<12} {p.name}")
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# repo list <project>
# ---------------------------------------------------------------------------


@repo_app.command("list")
def repo_list(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    limit: int = typer.Option(25, "--limit", help="Results per page"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """List repositories in a project."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        repos = client.list_repos(project, limit=limit)

        if fmt == OutputFormat.JSON or fmt == OutputFormat.RAW:
            typer.echo(format_output([r.model_dump() for r in repos], fmt))
        else:
            for r in repos:
                typer.echo(f"{r.slug:<30} {r.name}")
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# repo get <project> <slug>
# ---------------------------------------------------------------------------


@repo_app.command("get")
def repo_get(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    slug: str = typer.Argument(..., help="Repository slug"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Get a single repository."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        repo = client.get_repo(project, slug)

        if fmt == OutputFormat.JSON or fmt == OutputFormat.RAW:
            typer.echo(format_output(repo.model_dump(), fmt))
        else:
            lines = [
                f"slug:    {repo.slug}",
                f"name:    {repo.name}",
                f"project: {repo.project.key} ({repo.project.name})",
                f"state:   {repo.state or 'n/a'}",
                f"public:  {repo.public}",
            ]
            if repo.description:
                lines.append(f"desc:    {repo.description}")
            typer.echo("\n".join(lines))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# Phase 1 — PR read commands
# ---------------------------------------------------------------------------


@pr_app.command("list")
def pr_list(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    state: str | None = typer.Option(None, "--state", help="Filter by state: OPEN/MERGED/DECLINED/ALL"),
    limit: int = typer.Option(25, "--limit", help="Results per page"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """List pull requests in a repository."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        prs = client.list_pull_requests(project, repo, state=state, limit=limit)

        if fmt == OutputFormat.JSON or fmt == OutputFormat.RAW:
            typer.echo(format_output([p.model_dump() for p in prs], fmt))
        else:
            for p in prs:
                author = p.author.user.display_name if p.author else "?"
                typer.echo(f"PR-{p.id:<6} [{p.state:<8}] {p.title}  ({author})")
    except AtlasError as e:
        _handle_error(e, fmt)


@pr_app.command("get")
def pr_get(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Get a single pull request."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        pr = client.get_pull_request(project, repo, pr_id)

        if fmt == OutputFormat.JSON or fmt == OutputFormat.RAW:
            typer.echo(format_output(pr.model_dump(), fmt))
        else:
            author = pr.author.user.display_name if pr.author else "?"
            from_ref = pr.from_ref.display_id if pr.from_ref else "?"
            to_ref = pr.to_ref.display_id if pr.to_ref else "?"
            reviewers = ", ".join(r.user.display_name for r in pr.reviewers) or "none"
            lines = [
                f"id:        PR-{pr.id}",
                f"title:     {pr.title}",
                f"state:     {pr.state}",
                f"author:    {author}",
                f"from:      {from_ref}",
                f"to:        {to_ref}",
                f"reviewers: {reviewers}",
            ]
            if pr.description:
                lines.append(f"desc:\n{pr.description}")
            typer.echo("\n".join(lines))
    except AtlasError as e:
        _handle_error(e, fmt)


@pr_app.command("diff")
def pr_diff(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    path: str | None = typer.Option(None, "--path", help="Limit diff to a specific file path"),
    context_lines: int | None = typer.Option(None, "--context-lines", help="Lines of context around each change"),
    format: str | None = typer.Option(None, "--format", help="Override output format (md treated as raw)"),
) -> None:
    """Show the unified diff for a pull request."""
    ctx.ensure_object(dict)
    fmt = _diff_file_fmt(_resolve_fmt(ctx.obj, format))
    try:
        client = _make_client(ctx.obj)
        diff_text = client.get_pull_request_diff(project, repo, pr_id, path=path, context_lines=context_lines)
        typer.echo(diff_text)
    except AtlasError as e:
        _handle_error(e, fmt)


@pr_app.command("comments")
def pr_comments(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """List comments on a pull request."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        comments = client.list_pull_request_comments(project, repo, pr_id)

        if fmt == OutputFormat.JSON or fmt == OutputFormat.RAW:
            typer.echo(format_output([c.model_dump() for c in comments], fmt))
        else:
            for c in comments:
                author = c.author.display_name if c.author else "?"
                state_tag = f"[{c.state}] " if c.state and c.state != "OPEN" else ""
                typer.echo(f"#{c.id} {state_tag}{author}: {c.text or ''}")
    except AtlasError as e:
        _handle_error(e, fmt)


@pr_app.command("commits")
def pr_commits(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """List commits in a pull request."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        commits = client.list_pull_request_commits(project, repo, pr_id)

        if fmt == OutputFormat.JSON or fmt == OutputFormat.RAW:
            typer.echo(format_output([c.model_dump() for c in commits], fmt))
        else:
            for c in commits:
                author = c.author.display_name if c.author else "?"
                msg = (c.message or "").splitlines()[0] if c.message else ""
                typer.echo(f"{c.display_id}  {author}  {msg}")
    except AtlasError as e:
        _handle_error(e, fmt)


@pr_app.command("activity")
def pr_activity(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Show activity timeline for a pull request."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        activities = client.list_pull_request_activities(project, repo, pr_id)

        if fmt == OutputFormat.JSON or fmt == OutputFormat.RAW:
            typer.echo(format_output([a.model_dump() for a in activities], fmt))
        else:
            for a in activities:
                user = a.user.display_name if a.user else "?"
                typer.echo(f"#{a.id} {a.action}  {user}")
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# Phase 1 — branch commands
# ---------------------------------------------------------------------------


@branch_app.command("list")
def branch_list(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    filter: str | None = typer.Option(None, "--filter", help="Filter branches by text"),
    limit: int = typer.Option(25, "--limit", help="Results per page"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """List branches in a repository."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        branches = client.list_branches(project, repo, filter_text=filter, limit=limit)

        if fmt == OutputFormat.JSON or fmt == OutputFormat.RAW:
            typer.echo(format_output([b.model_dump() for b in branches], fmt))
        else:
            for b in branches:
                default_tag = " [default]" if b.is_default else ""
                typer.echo(f"{b.display_id}{default_tag}")
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# Phase 1 — file commands
# ---------------------------------------------------------------------------


@file_app.command("get")
def file_get(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    path: str = typer.Argument(..., help="File path within the repository"),
    ref: str | None = typer.Option(None, "--ref", help="Branch, tag, or commit ref"),
    format: str | None = typer.Option(None, "--format", help="Override output format (md treated as raw)"),
) -> None:
    """Get raw file content from a repository."""
    ctx.ensure_object(dict)
    fmt = _diff_file_fmt(_resolve_fmt(ctx.obj, format))
    try:
        client = _make_client(ctx.obj)
        content = client.get_file_content(project, repo, path, at=ref)
        typer.echo(content)
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# Phase 2 — PR write commands
# ---------------------------------------------------------------------------


@pr_app.command("create")
def pr_create(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    source: str = typer.Option(..., "--source", help="Source branch name"),
    target: str = typer.Option(..., "--target", help="Target branch name"),
    title: str = typer.Option(..., "--title", help="Pull request title"),
    description: str | None = typer.Option(None, "--description", help="Pull request description"),
    body_file: str | None = typer.Option(None, "--body-file", help="Description from file (- for stdin)"),
    reviewers: str | None = typer.Option(None, "--reviewers", help="Comma-separated reviewer usernames"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Create a new pull request."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        desc = description
        if body_file and desc is None:
            desc = read_body(body_file=body_file)
        reviewer_list = [r.strip() for r in reviewers.split(",") if r.strip()] if reviewers else None
        payload: dict[str, Any] = {
            "title": title,
            "fromRef": {"id": f"refs/heads/{source}"},
            "toRef": {"id": f"refs/heads/{target}"},
        }
        if desc:
            payload["description"] = desc
        if reviewer_list:
            payload["reviewers"] = [{"user": {"name": r}} for r in reviewer_list]
        if dry_run:
            url = f"{client.base_url}{client.API}/projects/{project}/repos/{repo}/pull-requests"
            typer.echo(format_dry_run("POST", url, body=payload, fmt=fmt.value))
            return
        result = client.create_pull_request(
            project, repo, title=title, from_ref=source, to_ref=target, description=desc, reviewers=reviewer_list
        )
        typer.echo(format_output(WriteResult(key=f"PR-{result.id}", action="created", summary=result.title), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@pr_app.command("update")
def pr_update(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    title: str | None = typer.Option(None, "--title", help="New title"),
    description: str | None = typer.Option(None, "--description", help="New description"),
    reviewers: str | None = typer.Option(None, "--reviewers", help="Comma-separated reviewer usernames"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Update an existing pull request."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        reviewer_list = [r.strip() for r in reviewers.split(",") if r.strip()] if reviewers else None
        payload: dict[str, Any] = {}
        if title:
            payload["title"] = title
        if description is not None:
            payload["description"] = description
        if reviewer_list is not None:
            payload["reviewers"] = [{"user": {"name": r}} for r in reviewer_list]
        if dry_run:
            url = f"{client.base_url}{client.API}/projects/{project}/repos/{repo}/pull-requests/{pr_id}"
            typer.echo(format_dry_run("PUT", url, body=payload, fmt=fmt.value))
            return
        result = client.update_pull_request(
            project, repo, pr_id, title=title, description=description, reviewers=reviewer_list
        )
        typer.echo(format_output(WriteResult(key=f"PR-{result.id}", action="updated", summary=result.title), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@pr_app.command("merge")
def pr_merge(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    strategy: str | None = typer.Option(None, "--strategy", help="Merge strategy: merge-commit/squash/ff-only"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Merge a pull request."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        if dry_run:
            url = f"{client.base_url}{client.API}/projects/{project}/repos/{repo}/pull-requests/{pr_id}/merge"
            payload: dict[str, Any] = {}
            if strategy:
                payload["strategyId"] = strategy
            typer.echo(format_dry_run("POST", url, body=payload or None, fmt=fmt.value))
            return
        result = client.merge_pull_request(project, repo, pr_id, strategy=strategy)
        typer.echo(format_output(WriteResult(key=f"PR-{result.id}", action="merged", summary=result.title), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@pr_app.command("decline")
def pr_decline(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Decline a pull request."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        if dry_run:
            url = f"{client.base_url}{client.API}/projects/{project}/repos/{repo}/pull-requests/{pr_id}/decline"
            typer.echo(format_dry_run("POST", url, fmt=fmt.value))
            return
        result = client.decline_pull_request(project, repo, pr_id)
        typer.echo(format_output(WriteResult(key=f"PR-{result.id}", action="declined", summary=result.title), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@pr_app.command("approve")
def pr_approve(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Approve a pull request."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        if dry_run:
            url = f"{client.base_url}{client.API}/projects/{project}/repos/{repo}/pull-requests/{pr_id}/approve"
            typer.echo(format_dry_run("POST", url, fmt=fmt.value))
            return
        participant = client.approve_pull_request(project, repo, pr_id)
        typer.echo(
            format_output(WriteResult(key=f"PR-{pr_id}", action="approved", summary=participant.user.display_name), fmt)
        )
    except AtlasError as e:
        _handle_error(e, fmt)


@pr_app.command("unapprove")
def pr_unapprove(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Remove approval from a pull request."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        if dry_run:
            url = f"{client.base_url}{client.API}/projects/{project}/repos/{repo}/pull-requests/{pr_id}/approve"
            typer.echo(format_dry_run("DELETE", url, fmt=fmt.value))
            return
        client.unapprove_pull_request(project, repo, pr_id)
        typer.echo(format_output(WriteResult(key=f"PR-{pr_id}", action="unapproved", summary=None), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@pr_app.command("needs-work")
def pr_needs_work(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Mark a pull request as needs-work."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        if dry_run:
            slug = client._get_current_user_slug()
            url = (
                f"{client.base_url}{client.API}/projects/{project}/repos/{repo}"
                f"/pull-requests/{pr_id}/participants/{slug}"
            )
            typer.echo(format_dry_run("PUT", url, body={"status": "NEEDS_WORK"}, fmt=fmt.value))
            return
        client.needs_work_pull_request(project, repo, pr_id)
        typer.echo(format_output(WriteResult(key=f"PR-{pr_id}", action="needs-work", summary=None), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@pr_app.command("reopen")
def pr_reopen(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Reopen a declined pull request."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        if dry_run:
            url = f"{client.base_url}{client.API}/projects/{project}/repos/{repo}/pull-requests/{pr_id}/reopen"
            typer.echo(format_dry_run("POST", url, fmt=fmt.value))
            return
        result = client.reopen_pull_request(project, repo, pr_id)
        typer.echo(format_output(WriteResult(key=f"PR-{result.id}", action="reopened", summary=result.title), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# Phase 2 — comment write commands
# ---------------------------------------------------------------------------


@comment_app.command("add")
def comment_add(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    body_file: str = typer.Option(..., "--body-file", help="Comment body file (- for stdin)"),
    inline_path: str | None = typer.Option(None, "--inline-path", help="File path for inline comment"),
    inline_to: int | None = typer.Option(None, "--inline-to", help="Line number for inline comment"),
    inline_type: str | None = typer.Option(None, "--inline-type", help="Line type: ADDED/REMOVED/CONTEXT"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Add a comment to a pull request."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        text = read_body(body_file=body_file)
        anchor: dict[str, Any] | None = None
        if inline_path:
            anchor = {"path": inline_path}
            if inline_to is not None:
                anchor["line"] = inline_to
            if inline_type:
                anchor["lineType"] = inline_type.upper()
        payload: dict[str, Any] = {"text": text}
        if anchor:
            payload["anchor"] = anchor
        if dry_run:
            url = f"{client.base_url}{client.API}/projects/{project}/repos/{repo}/pull-requests/{pr_id}/comments"
            typer.echo(format_dry_run("POST", url, body=payload, fmt=fmt.value))
            return
        result = client.add_pull_request_comment(project, repo, pr_id, text=text, anchor=anchor)
        typer.echo(
            format_output(
                WriteResult(key=f"PR-{pr_id}#comment-{result.id}", action="created", summary=result.text), fmt
            )
        )
    except AtlasError as e:
        _handle_error(e, fmt)


@comment_app.command("reply")
def comment_reply(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    comment_id: int = typer.Argument(..., help="Parent comment ID"),
    body_file: str = typer.Option(..., "--body-file", help="Reply body file (- for stdin)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Reply to a comment on a pull request."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        text = read_body(body_file=body_file)
        payload: dict[str, Any] = {"text": text, "parent": {"id": comment_id}}
        if dry_run:
            url = f"{client.base_url}{client.API}/projects/{project}/repos/{repo}/pull-requests/{pr_id}/comments"
            typer.echo(format_dry_run("POST", url, body=payload, fmt=fmt.value))
            return
        result = client.reply_to_comment(project, repo, pr_id, comment_id, text=text)
        typer.echo(
            format_output(
                WriteResult(key=f"PR-{pr_id}#comment-{result.id}", action="created", summary=result.text), fmt
            )
        )
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# Phase 3 — comment CRUD
# ---------------------------------------------------------------------------


@comment_app.command("update")
def comment_update(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    comment_id: int = typer.Argument(..., help="Comment ID"),
    body_file: str | None = typer.Option(None, "--body-file", help="New body file (- for stdin)"),
    version: int | None = typer.Option(None, "--version", help="Comment version for optimistic locking"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
) -> None:
    """Update a comment on a pull request."""
    ctx.ensure_object(dict)
    fmt = _fmt(ctx.obj)
    try:
        client = _make_client(ctx.obj)
        text = read_body(body_file=body_file)
        payload: dict[str, Any] = {"text": text}
        if version is not None:
            payload["version"] = version
        if dry_run:
            url = (
                f"{client.base_url}{client.API}/projects/{project}/repos/{repo}"
                f"/pull-requests/{pr_id}/comments/{comment_id}"
            )
            typer.echo(format_dry_run("PUT", url, body=payload, fmt=fmt.value))
            return
        result = client.update_comment(project, repo, pr_id, comment_id, text=text, version=version)
        typer.echo(
            format_output(
                WriteResult(key=f"PR-{pr_id}#comment-{result.id}", action="updated", summary=result.text), fmt
            )
        )
    except AtlasError as e:
        _handle_error(e, fmt)


@comment_app.command("delete")
def comment_delete(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    comment_id: int = typer.Argument(..., help="Comment ID"),
    version: int | None = typer.Option(None, "--version", help="Comment version for optimistic locking"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
) -> None:
    """Delete a comment on a pull request."""
    ctx.ensure_object(dict)
    fmt = _fmt(ctx.obj)
    try:
        client = _make_client(ctx.obj)
        if dry_run:
            url = (
                f"{client.base_url}{client.API}/projects/{project}/repos/{repo}"
                f"/pull-requests/{pr_id}/comments/{comment_id}"
            )
            params: dict[str, Any] = {}
            if version is not None:
                params["version"] = version
            typer.echo(format_dry_run("DELETE", url, body=params or None, fmt=fmt.value))
            return
        client.delete_comment(project, repo, pr_id, comment_id, version=version)
        typer.echo(
            format_output(WriteResult(key=f"PR-{pr_id}#comment-{comment_id}", action="deleted", summary=None), fmt)
        )
    except AtlasError as e:
        _handle_error(e, fmt)


@comment_app.command("resolve")
def comment_resolve(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    comment_id: int = typer.Argument(..., help="Comment ID"),
    version: int | None = typer.Option(None, "--version", help="Comment version for optimistic locking"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
) -> None:
    """Resolve a comment on a pull request."""
    ctx.ensure_object(dict)
    fmt = _fmt(ctx.obj)
    try:
        client = _make_client(ctx.obj)
        if dry_run:
            url = (
                f"{client.base_url}{client.API}/projects/{project}/repos/{repo}"
                f"/pull-requests/{pr_id}/comments/{comment_id}"
            )
            typer.echo(format_dry_run("PUT", url, body={"state": "RESOLVED"}, fmt=fmt.value))
            return
        result = client.resolve_comment(project, repo, pr_id, comment_id, version=version)
        typer.echo(
            format_output(
                WriteResult(key=f"PR-{pr_id}#comment-{result.id}", action="resolved", summary=result.text), fmt
            )
        )
    except AtlasError as e:
        _handle_error(e, fmt)


@comment_app.command("reopen")
def comment_reopen(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    comment_id: int = typer.Argument(..., help="Comment ID"),
    version: int | None = typer.Option(None, "--version", help="Comment version for optimistic locking"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
) -> None:
    """Reopen a resolved comment on a pull request."""
    ctx.ensure_object(dict)
    fmt = _fmt(ctx.obj)
    try:
        client = _make_client(ctx.obj)
        if dry_run:
            url = (
                f"{client.base_url}{client.API}/projects/{project}/repos/{repo}"
                f"/pull-requests/{pr_id}/comments/{comment_id}"
            )
            typer.echo(format_dry_run("PUT", url, body={"state": "OPEN"}, fmt=fmt.value))
            return
        result = client.reopen_comment(project, repo, pr_id, comment_id, version=version)
        typer.echo(
            format_output(
                WriteResult(key=f"PR-{pr_id}#comment-{result.id}", action="reopened", summary=result.text), fmt
            )
        )
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# Phase 3 — task commands
# ---------------------------------------------------------------------------


@task_app.command("list")
def task_list(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """List tasks on a pull request."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        tasks = client.list_tasks(project, repo, pr_id)

        if fmt == OutputFormat.JSON or fmt == OutputFormat.RAW:
            typer.echo(format_output([t.model_dump() for t in tasks], fmt))
        else:
            for t in tasks:
                author = t.author.display_name if t.author else "?"
                typer.echo(f"#{t.id} [{t.state}] {t.text}  ({author})")
    except AtlasError as e:
        _handle_error(e, fmt)


@task_app.command("get")
def task_get(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    task_id: int = typer.Argument(..., help="Task ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Get a single task from a pull request (filters list by task ID)."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        tasks = client.list_tasks(project, repo, pr_id)
        task = next((t for t in tasks if t.id == task_id), None)
        if task is None:
            typer.echo(f"Error: Task {task_id} not found on PR-{pr_id}", err=True)
            raise typer.Exit(2)
        if fmt == OutputFormat.JSON or fmt == OutputFormat.RAW:
            typer.echo(format_output(task.model_dump(), fmt))
        else:
            author = task.author.display_name if task.author else "?"
            typer.echo(f"#{task.id} [{task.state}] {task.text}  ({author})")
    except AtlasError as e:
        _handle_error(e, fmt)


@task_app.command("create")
def task_create(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    text: str = typer.Option(..., "--text", help="Task text"),
    comment_id: int = typer.Option(..., "--comment-id", help="Comment ID to anchor the task"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
) -> None:
    """Create a task anchored to a comment on a pull request."""
    ctx.ensure_object(dict)
    fmt = _fmt(ctx.obj)
    try:
        client = _make_client(ctx.obj)
        payload: dict[str, Any] = {
            "anchor": {"id": comment_id, "type": "COMMENT"},
            "text": text,
        }
        if dry_run:
            url = f"{client.base_url}{client.API}/tasks"
            typer.echo(format_dry_run("POST", url, body=payload, fmt=fmt.value))
            return
        result = client.create_task(text=text, comment_id=comment_id)
        typer.echo(
            format_output(WriteResult(key=f"PR-{pr_id}#task-{result.id}", action="created", summary=result.text), fmt)
        )
    except AtlasError as e:
        _handle_error(e, fmt)


@task_app.command("update")
def task_update(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    task_id: int = typer.Argument(..., help="Task ID"),
    state: str | None = typer.Option(None, "--state", help="New state: OPEN/RESOLVED"),
    text: str | None = typer.Option(None, "--text", help="New task text"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
) -> None:
    """Update a task on a pull request."""
    ctx.ensure_object(dict)
    fmt = _fmt(ctx.obj)
    try:
        client = _make_client(ctx.obj)
        payload: dict[str, Any] = {}
        if state:
            payload["state"] = state.upper()
        if text:
            payload["text"] = text
        if dry_run:
            url = f"{client.base_url}{client.API}/tasks/{task_id}"
            typer.echo(format_dry_run("PUT", url, body=payload, fmt=fmt.value))
            return
        result = client.update_task(task_id, state=state, text=text)
        typer.echo(
            format_output(WriteResult(key=f"PR-{pr_id}#task-{result.id}", action="updated", summary=result.text), fmt)
        )
    except AtlasError as e:
        _handle_error(e, fmt)


@task_app.command("delete")
def task_delete(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    task_id: int = typer.Argument(..., help="Task ID"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
) -> None:
    """Delete a task from a pull request."""
    ctx.ensure_object(dict)
    fmt = _fmt(ctx.obj)
    try:
        client = _make_client(ctx.obj)
        if dry_run:
            url = f"{client.base_url}{client.API}/tasks/{task_id}"
            typer.echo(format_dry_run("DELETE", url, fmt=fmt.value))
            return
        client.delete_task(task_id)
        typer.echo(format_output(WriteResult(key=f"PR-{pr_id}#task-{task_id}", action="deleted", summary=None), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# Phase 3 — PR supplemental commands
# ---------------------------------------------------------------------------


@pr_app.command("diffstat")
def pr_diffstat(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Show file-level change stats for a pull request."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        stats = client.get_pull_request_diffstat(project, repo, pr_id)

        if fmt == OutputFormat.JSON or fmt == OutputFormat.RAW:
            typer.echo(format_output([s.model_dump() for s in stats], fmt))
        else:
            for s in stats:
                src = s.src_path.to_string if s.src_path else None
                dest = s.path.to_string
                change_type = s.type or "?"
                if src and src != dest:
                    typer.echo(f"{change_type:<8} {src} → {dest}")
                else:
                    typer.echo(f"{change_type:<8} {dest}")
    except AtlasError as e:
        _handle_error(e, fmt)


@pr_app.command("statuses")
def pr_statuses(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project key"),
    repo: str = typer.Argument(..., help="Repository slug"),
    pr_id: int = typer.Argument(..., help="Pull request ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Show build statuses for a pull request's source commit."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        pr = client.get_pull_request(project, repo, pr_id)
        if pr.from_ref is None or pr.from_ref.latest_commit is None:
            typer.echo("Error: PR has no source commit hash", err=True)
            raise typer.Exit(2)
        commit_hash = pr.from_ref.latest_commit
        statuses = client.get_build_statuses(commit_hash)

        if fmt == OutputFormat.JSON or fmt == OutputFormat.RAW:
            typer.echo(format_output([s.model_dump() for s in statuses], fmt))
        else:
            for s in statuses:
                name = s.name or s.key
                typer.echo(f"{s.state:<12} {name}")
    except AtlasError as e:
        _handle_error(e, fmt)


@pr_app.command("pending-review")
def pr_pending_review(
    ctx: typer.Context,
    state: str | None = typer.Option(None, "--state", help="Filter by state: OPEN/MERGED/DECLINED"),
    limit: int = typer.Option(25, "--limit", help="Results per page"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """List pull requests where you are a reviewer."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        prs = client.list_pull_requests_for_reviewer(state=state, limit=limit)

        if fmt == OutputFormat.JSON or fmt == OutputFormat.RAW:
            typer.echo(format_output([p.model_dump() for p in prs], fmt))
        else:
            for p in prs:
                author = p.author.user.display_name if p.author else "?"
                repo_slug = p.from_ref.repository.slug if p.from_ref and p.from_ref.repository else "?"
                typer.echo(f"PR-{p.id:<6} [{p.state:<8}] {repo_slug}  {p.title}  ({author})")
    except AtlasError as e:
        _handle_error(e, fmt)
