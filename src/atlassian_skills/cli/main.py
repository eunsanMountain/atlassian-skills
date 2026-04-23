from __future__ import annotations

import contextlib
import sys

import typer

from atlassian_skills import __version__
from atlassian_skills.core.format import OutputFormat


def _configure_windows_encoding() -> None:
    """Reconfigure stdio to UTF-8 on Windows so cp949/cp932/gbk consoles don't crash.

    Windows CJK locales (`sys.stdout.encoding == "cp949"` on Korean Windows) cannot
    represent common Atlassian body characters like em dash (U+2014), curly quotes,
    and ellipsis. Without this reconfigure, `atls ... --format=md` raises
    `UnicodeEncodeError` on any body that has been through Atlassian's text auto-format.
    `errors="replace"` is a last-resort guard for legacy consoles that truly can't
    render UTF-8 — they fall back to `?` instead of crashing.
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        with contextlib.suppress(AttributeError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")


_configure_windows_encoding()


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"atls {__version__}")
        raise typer.Exit()


app = typer.Typer(
    name="atls",
    help="Token-efficient CLI for Atlassian Server/DC. Use --format globally before a subcommand or locally as --format on Jira/Confluence commands.",
    no_args_is_help=True,
)


@app.callback()
def main(
    ctx: typer.Context,
    profile: str = typer.Option("default", "--profile", "-p", help="Config profile name"),
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_callback, is_eager=True, help="Show version"
    ),
    format: str = typer.Option(
        "compact",
        "--format",
        "-f",
        help="Output format: compact|json|md|raw. Can be set globally here or locally on Jira/Confluence commands.",
    ),
    fields: str | None = typer.Option(None, "--fields", help="Comma-separated fields to include"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress non-essential output"),
    verbose: int = typer.Option(0, "--verbose", help="Verbosity level (0-3)"),
    timeout: float = typer.Option(30.0, "--timeout", help="Request timeout in seconds"),
) -> None:
    ctx.ensure_object(dict)
    try:
        fmt = OutputFormat(format)
    except ValueError:
        valid = ", ".join(f.value for f in OutputFormat)
        typer.echo(f"Error: Invalid format {format!r}. Valid: {valid}", err=True)
        raise typer.Exit(1) from None
    ctx.obj["profile"] = profile
    ctx.obj["format"] = fmt
    ctx.obj["fields"] = [f.strip() for f in fields.split(",") if f.strip()] if fields else None
    ctx.obj["quiet"] = quiet
    ctx.obj["verbose"] = verbose
    ctx.obj["timeout"] = timeout


def _register_sub_apps() -> None:
    """Deferred import to avoid circular imports at module load time."""
    from atlassian_skills.cli.auth import auth_app
    from atlassian_skills.cli.bitbucket import bitbucket_app
    from atlassian_skills.cli.config import config_app
    from atlassian_skills.cli.confluence import confluence_app
    from atlassian_skills.cli.jira import jira_app
    from atlassian_skills.cli.setup import setup_app
    from atlassian_skills.cli.upgrade import upgrade
    from atlassian_skills.cli.version import version
    from atlassian_skills.cli.zephyr import zephyr_app

    app.add_typer(auth_app, name="auth")
    app.add_typer(bitbucket_app, name="bitbucket")
    app.add_typer(config_app, name="config")
    app.add_typer(confluence_app, name="confluence")
    app.add_typer(jira_app, name="jira")
    app.add_typer(setup_app, name="setup")
    app.add_typer(zephyr_app, name="zephyr")
    app.command("upgrade")(upgrade)
    app.command("version")(version)


_register_sub_apps()
