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


def latest_pypi_version(timeout: float = DEFAULT_TIMEOUT) -> str | None:
    """Return the latest atlassian-skills version published on PyPI, or None if the check
    can't complete (offline, timeout, HTTP error, malformed response). Never raises — callers
    treat None as "couldn't check"."""
    try:
        response = httpx.get(PYPI_URL, timeout=timeout, headers={"Accept": "application/json"})
        response.raise_for_status()
        return str(response.json()["info"]["version"])
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        return None


def is_outdated(current: str, latest: str) -> bool:
    """True when `latest` is a strictly newer version than `current`."""
    return _parse_version(latest) > _parse_version(current)


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

    latest = latest_pypi_version(timeout)
    if latest is None:
        typer.echo(f"atls {__version__}  (update check failed)")
        return

    if is_outdated(__version__, latest):
        typer.echo(f"atls {__version__} — latest {latest} available. Run 'atls upgrade' to update.")
        raise typer.Exit(1)
    typer.echo(f"atls {__version__} (latest)")
