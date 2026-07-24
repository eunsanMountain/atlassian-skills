"""Canonical path keys and native stable file identity."""

from __future__ import annotations

import ntpath
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from atlassian_skills.core.errors import ValidationError


@dataclass(frozen=True)
class CanonicalPath:
    display_path: str
    canonical_key: str


@dataclass(frozen=True)
class StableFileIdentity:
    platform: str
    primary: int
    secondary: int
    link_count: int

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.primary:x}:{self.secondary:x}"


def _has_windows_reparse_attribute(info: os.stat_result) -> bool:
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_point = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    return bool(attributes & reparse_point)


def canonical_path(path: Path, *, platform: str | None = None) -> CanonicalPath:
    resolved = path.expanduser().resolve(strict=False)
    display = str(resolved)
    selected = platform or ("windows" if os.name == "nt" else "posix")
    key = ntpath.normcase(display) if selected == "windows" else display
    return CanonicalPath(display_path=display, canonical_key=key)


def _windows_identity(path: Path, link_count: int) -> StableFileIdentity:
    # CPython exposes the native Windows file index as st_ino and the volume
    # serial as st_dev.  Both values come from the opened file handle and are
    # stable across renames on the same volume.
    info = path.stat(follow_symlinks=False)
    if not info.st_dev or not info.st_ino:
        raise ValidationError(
            "Windows filesystem did not provide a stable file identity",
            hint="Use an NTFS/ReFS local path and retry.",
            context={"reason": "unsupported-file-identity"},
        )
    return StableFileIdentity("windows", int(info.st_dev), int(info.st_ino), link_count)


def inspect_file_identity(
    path: Path,
    *,
    reject_links: bool = True,
) -> StableFileIdentity:
    try:
        info = path.lstat()
    except OSError as error:
        raise ValidationError(
            "Managed file identity could not be read",
            context={"reason": "unsupported-file-identity", "path": str(path)},
        ) from error
    if reject_links and os.name == "nt" and _has_windows_reparse_attribute(info):
        raise ValidationError(
            "Managed file cannot be a Windows reparse point",
            context={"reason": "reparse-point-file"},
        )
    if not stat.S_ISREG(info.st_mode):
        raise ValidationError("Managed path is not a regular file", context={"reason": "unsupported-file-identity"})
    if reject_links and stat.S_ISLNK(info.st_mode):
        raise ValidationError("Managed file cannot be a symbolic link", context={"reason": "symlink-file"})
    if reject_links and info.st_nlink != 1:
        raise ValidationError(
            "Managed file cannot have multiple hard links",
            context={"reason": "duplicate-file-identity", "link_count": info.st_nlink},
        )
    if os.name == "nt":
        return _windows_identity(path, int(info.st_nlink))
    if not info.st_dev or not info.st_ino:
        raise ValidationError(
            "Filesystem did not provide a stable inode", context={"reason": "unsupported-file-identity"}
        )
    return StableFileIdentity("posix", int(info.st_dev), int(info.st_ino), int(info.st_nlink))


def assert_distinct_identities(paths: list[Path]) -> dict[str, StableFileIdentity]:
    observed: dict[str, StableFileIdentity] = {}
    identities: dict[str, str] = {}
    for path in paths:
        canonical = canonical_path(path)
        identity = inspect_file_identity(path)
        previous = identities.get(identity.key)
        if previous is not None and previous != canonical.canonical_key:
            raise ValidationError(
                "Two managed paths refer to the same file identity",
                context={"reason": "duplicate-file-identity", "paths": [previous, canonical.canonical_key]},
            )
        identities[identity.key] = canonical.canonical_key
        observed[canonical.canonical_key] = identity
    return observed


__all__ = [
    "CanonicalPath",
    "StableFileIdentity",
    "assert_distinct_identities",
    "canonical_path",
    "inspect_file_identity",
]
