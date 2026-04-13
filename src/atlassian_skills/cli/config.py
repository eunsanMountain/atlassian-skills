from __future__ import annotations

import typer

from atlassian_skills.core.config import config_path, load_config, save_config

config_app = typer.Typer(help="Manage atlassian-skills configuration", no_args_is_help=True)


@config_app.command("path")
def config_path_cmd() -> None:
    """Print the path to the configuration file."""
    typer.echo(str(config_path()))


@config_app.command("get")
def config_get(
    key: str = typer.Argument(..., help="Dot-separated config key, e.g. profiles.corp.jira_url"),
) -> None:
    """Print a specific configuration value."""
    config = load_config()
    data = config.model_dump()
    parts = key.split(".")
    node: object = data
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            typer.echo(f"Key not found: {key}", err=True)
            raise typer.Exit(1)
        node = node[part]
    typer.echo(str(node))


def _validate_config_key(key: str) -> None:
    """Validate that the config key is an allowed path."""
    if key == "default_profile":
        return
    if key.startswith("profiles."):
        parts = key.split(".")
        # profiles.<name>.<field> or profiles.<name>.auth.<product>
        if len(parts) >= 3:
            return
    raise typer.Exit(1)


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Dot-separated config key, e.g. profiles.corp.jira_url"),
    value: str = typer.Argument(..., help="Value to set"),
) -> None:
    """Set a configuration value and save it to disk."""
    if not (key == "default_profile" or key.startswith("profiles.")):
        typer.echo(f"Invalid config key: {key!r}. Must be 'default_profile' or 'profiles.<name>.<field>'.", err=True)
        raise typer.Exit(1)
    config = load_config()
    data = config.model_dump()
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        if not isinstance(node, dict):
            typer.echo(f"Key path invalid at segment '{part}'", err=True)
            raise typer.Exit(1)
        if part not in node:
            node[part] = {}
        node = node[part]
    if not isinstance(node, dict):
        typer.echo(f"Cannot set value at '{key}'", err=True)
        raise typer.Exit(1)
    node[parts[-1]] = value

    from atlassian_skills.core.config import Config
    updated = Config.model_validate(data)
    save_config(updated)
    typer.echo(f"Set {key} = {value}")
