from __future__ import annotations

import os

import typer

from atlassian_skills.core.config import get_env_token, get_profile, load_config

auth_app = typer.Typer(help="Manage authentication credentials", no_args_is_help=True)


def _resolve_url(profile_name: str, product: str, profile_url: str | None) -> tuple[str | None, str | None]:
    """Return (url, source) where source is 'config', 'env', or None."""
    if profile_url:
        return profile_url, "config"
    env_key = f"ATLS_{profile_name.upper()}_{product.upper()}_URL"
    env_val = os.environ.get(env_key)
    if env_val:
        return env_val, f"env ({env_key})"
    return None, None


@auth_app.command("login")
def auth_login(
    ctx: typer.Context,
    profile: str = typer.Option(None, "--profile", "-p", help="Profile name (overrides global)"),
    product: str = typer.Option("jira", "--product", help="Product: jira|confluence|bitbucket|bamboo"),
) -> None:
    """Show export snippet for setting up authentication tokens."""
    ctx.ensure_object(dict)
    profile_name = profile or ctx.obj.get("profile", "default")
    upper = f"ATLS_{profile_name.upper()}_{product.upper()}"
    typer.echo("# Set the following environment variables to authenticate:")
    typer.echo(f"export {upper}_URL=<your-server-url>")
    typer.echo(f"export {upper}_TOKEN=<your-personal-access-token>")
    typer.echo()
    typer.echo("# For basic auth, also set:")
    typer.echo(f"export {upper}_USER=<your-username>")
    typer.echo(f"export {upper}_AUTH=basic")


@auth_app.command("status")
def auth_status(
    ctx: typer.Context,
    profile: str = typer.Option(None, "--profile", "-p", help="Profile name (overrides global)"),
) -> None:
    """Report credential source and connectivity for the current profile."""
    ctx.ensure_object(dict)
    profile_name = profile or ctx.obj.get("profile", "default")
    config = load_config()
    prof = get_profile(config, profile_name)

    products = ("jira", "confluence", "bitbucket")
    url_fields = {"jira": prof.jira_url, "confluence": prof.confluence_url, "bitbucket": prof.bitbucket_url}

    typer.echo(f"Profile: {profile_name}")
    for product in products:
        url, src = _resolve_url(profile_name, product, url_fields[product])
        label = f"{product.capitalize()} URL:"
        typer.echo(f"  {label:<18}{url or '(not configured)'}{f'  ({src})' if src else ''}")

    auth_parts = " / ".join(f"{getattr(prof.auth, p)} ({p})" for p in products)
    typer.echo(f"  Auth method:    {auth_parts}")
    typer.echo(f"  Storage:        {prof.storage}")

    for product in products:
        token = get_env_token(profile_name, product)
        if token:
            typer.echo(f"  [{product}] token: set (length={len(token)})")
        else:
            typer.echo(f"  [{product}] token: NOT SET")


@auth_app.command("list")
def auth_list(ctx: typer.Context) -> None:
    """List all configured profiles with their storage mode."""
    config = load_config()
    if not config.profiles:
        typer.echo("No profiles in config.toml. Using env-based 'default' profile.")
        typer.echo("  Run 'atls auth status' to see the resolved configuration.")
        return
    for name, prof in config.profiles.items():
        marker = " (default)" if name == config.default_profile else ""
        urls = []
        for product, profile_url in [
            ("jira", prof.jira_url),
            ("confluence", prof.confluence_url),
            ("bitbucket", prof.bitbucket_url),
        ]:
            url, _ = _resolve_url(name, product, profile_url)
            urls.append(f"{product}={url or 'n/a'}")
        typer.echo(f"  {name}{marker}  storage={prof.storage}  {'  '.join(urls)}")
