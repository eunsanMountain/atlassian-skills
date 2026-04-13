from __future__ import annotations

import typer

from atlassian_skills.core.config import get_env_token, get_profile, load_config

auth_app = typer.Typer(help="Manage authentication credentials", no_args_is_help=True)


@auth_app.command("login")
def auth_login(
    ctx: typer.Context,
    profile: str = typer.Option(None, "--profile", "-p", help="Profile name (overrides global)"),
    product: str = typer.Option("jira", "--product", help="Product: jira|confluence|bitbucket|bamboo"),
) -> None:
    """Show export snippet for setting up authentication tokens."""
    ctx.ensure_object(dict)
    profile_name = profile or ctx.obj.get("profile", "default")
    env_key = f"ATLS_{profile_name.upper()}_{product.upper()}_TOKEN"
    typer.echo("# Set the following environment variable to authenticate:")
    typer.echo(f"export {env_key}=<your-personal-access-token>")
    typer.echo()
    typer.echo("# For basic auth, also set:")
    typer.echo(f"export ATLS_{profile_name.upper()}_{product.upper()}_USER=<your-username>")
    typer.echo(f"export ATLS_{profile_name.upper()}_{product.upper()}_AUTH=basic")


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

    typer.echo(f"Profile: {profile_name}")
    typer.echo(f"  Jira URL:      {prof.jira_url or '(not configured)'}")
    typer.echo(f"  Confluence URL:{prof.confluence_url or '(not configured)'}")
    typer.echo(f"  Auth method:   {prof.auth.jira} (jira) / {prof.auth.confluence} (confluence)")
    typer.echo(f"  Storage:       {prof.storage}")

    for product in ("jira", "confluence"):
        token = get_env_token(profile_name, product)
        env_key = f"ATLS_{profile_name.upper()}_{product.upper()}_TOKEN"
        if token:
            typer.echo(f"  [{product}] token: set via {env_key} (length={len(token)})")
        else:
            typer.echo(f"  [{product}] token: NOT SET — export {env_key}=<token>")


@auth_app.command("list")
def auth_list(ctx: typer.Context) -> None:
    """List all configured profiles with their storage mode."""
    config = load_config()
    if not config.profiles:
        typer.echo("No profiles configured. Edit ~/.config/atlassian-skills/config.toml to add profiles.")
        return
    for name, prof in config.profiles.items():
        marker = " (default)" if name == config.default_profile else ""
        typer.echo(f"  {name}{marker}  storage={prof.storage}  jira_url={prof.jira_url or 'n/a'}")
