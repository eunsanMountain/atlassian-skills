from __future__ import annotations

import json
import os
from typing import Any

import typer

from atlassian_skills.bitbucket.client import BitbucketClient
from atlassian_skills.core.auth import resolve_credential
from atlassian_skills.core.config import get_profile, load_config
from atlassian_skills.core.errors import AtlasError
from atlassian_skills.core.format import OutputFormat, format_output

bitbucket_app = typer.Typer(help="Bitbucket commands", no_args_is_help=True)

# Sub-groups
project_app = typer.Typer(help="Project commands", no_args_is_help=True)
repo_app = typer.Typer(help="Repository commands", no_args_is_help=True)

bitbucket_app.add_typer(project_app, name="project")
bitbucket_app.add_typer(repo_app, name="repo")


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
