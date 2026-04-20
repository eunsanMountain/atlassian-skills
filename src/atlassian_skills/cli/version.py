from __future__ import annotations

import httpx
import typer

from atlassian_skills import __version__

PYPI_URL = "https://pypi.org/pypi/atlassian-skills/json"
DEFAULT_TIMEOUT = 2.0


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a dotted version like '0.2.1' or '1.0.0rc1' into a comparable tuple of ints."""
    parts: list[int] = []
    for p in v.split("."):
        digits = ""
        for ch in p:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def version(
    check: bool = typer.Option(
        False,
        "--check",
        help="Query PyPI for the latest release and compare with the installed version (opt-in).",
    ),
    timeout: float = typer.Option(
        DEFAULT_TIMEOUT,
        "--timeout",
        help="PyPI request timeout in seconds.",
    ),
) -> None:
    """Show the installed version. With --check, compare against the latest PyPI release."""
    if not check:
        typer.echo(f"atls {__version__}")
        return

    try:
        response = httpx.get(PYPI_URL, timeout=timeout, headers={"Accept": "application/json"})
        response.raise_for_status()
        latest = str(response.json()["info"]["version"])
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        typer.echo(f"atls {__version__}  (update check failed: {exc})")
        return

    if _parse_version(latest) > _parse_version(__version__):
        typer.echo(f"atls {__version__} — latest {latest} available. Run 'atls upgrade' to update.")
        raise typer.Exit(1)
    typer.echo(f"atls {__version__} (latest)")
