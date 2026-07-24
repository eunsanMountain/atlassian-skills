"""Fail closed unless a package version is absent from the PyPI JSON API."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

_PYPI_API = "https://pypi.org/pypi/{package}/{version}/json"
_TIMEOUT_SECONDS = 10.0


class PyPIVersionGuardError(RuntimeError):
    """The registry cannot prove that a release version is absent."""


def _version_url(package: str, version: str) -> str:
    return _PYPI_API.format(package=package, version=version)


def ensure_version_absent(
    package: str,
    version: str,
    *,
    opener: Callable[..., AbstractContextManager[object]] = urlopen,
    timeout: float = _TIMEOUT_SECONDS,
) -> None:
    """Return only when PyPI answers that ``package==version`` does not exist.

    Any existing version, unexpected response, or network failure blocks the
    release. A version must never be republished from a differently tagged tree.
    """

    url = _version_url(package, version)
    try:
        with opener(url, timeout=timeout) as response:
            status = getattr(response, "status", None)
    except HTTPError as error:
        if error.code == 404:
            return
        raise PyPIVersionGuardError(f"PyPI version endpoint returned HTTP {error.code}") from error
    except (OSError, URLError) as error:
        raise PyPIVersionGuardError("PyPI version endpoint is unavailable") from error

    if status == 404:
        return
    if status is None:
        raise PyPIVersionGuardError("PyPI version endpoint returned no HTTP status")
    raise PyPIVersionGuardError(f"PyPI version endpoint returned HTTP {status}")


def main(argv: Sequence[str] | None = None, *, opener: Callable[..., AbstractContextManager[object]] = urlopen) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2:
        print("usage: check_pypi_version_absent.py PACKAGE VERSION", file=sys.stderr)
        return 2

    package, version = arguments
    try:
        ensure_version_absent(package, version, opener=opener)
    except PyPIVersionGuardError as error:
        print(f"::error::{error}; refusing to publish", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
