"""Structured UTF-8 reads for managed Markdown command boundaries."""

from __future__ import annotations

import stat
from pathlib import Path, PurePosixPath

from atlassian_skills.core.directory_capability import DirectoryCapability
from atlassian_skills.core.errors import ValidationError


def read_managed_utf8(path: Path, *, reason: str = "managed_file_read_failed") -> str:
    """Read managed Markdown without leaking filesystem or decoding exceptions."""

    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValidationError(
            "Managed Markdown must be a readable UTF-8 file",
            context={"reason": reason, "path": str(path), "failure": type(error).__name__},
        ) from error


def read_managed_utf8_bound(
    capability: DirectoryCapability,
    path: Path,
    *,
    reason: str = "managed_file_read_failed",
) -> str:
    """Read one managed leaf through an already acquired directory capability."""

    try:
        with capability.open_readonly(path.name) as stream:
            return stream.read().decode("utf-8")
    except ValidationError:
        raise
    except (OSError, UnicodeError) as error:
        raise ValidationError(
            "Managed Markdown must be a readable UTF-8 file",
            context={"reason": reason, "path": str(path), "failure": type(error).__name__},
        ) from error


def resolve_managed_asset_path(managed_path: Path, src: str) -> Path:
    """Resolve a portable asset reference without trusting symlink/reparse ancestors."""

    relative = PurePosixPath(src)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValidationError("Managed asset path is not portable", context={"reason": "unsafe_asset_path"})
    candidate = managed_path.parent.joinpath(*relative.parts)
    current = managed_path.parent
    for part in relative.parts[:-1]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        attributes = int(getattr(info, "st_file_attributes", 0))
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
        if stat.S_ISLNK(info.st_mode) or attributes & reparse_flag:
            raise ValidationError(
                "Managed asset path contains a symlink or reparse-point ancestor",
                context={"reason": "unsafe_asset_path", "path": str(current)},
            )
    root = managed_path.parent.resolve(strict=False)
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValidationError("Managed asset escapes its Markdown directory", context={"reason": "unsafe_asset_path"})
    return candidate


__all__ = ["read_managed_utf8", "read_managed_utf8_bound", "resolve_managed_asset_path"]
