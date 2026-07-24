"""State-free, capability-bound publication of managed local files."""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path

from atlassian_skills.core.attachment_io import AttachmentWriteBatch
from atlassian_skills.core.directory_capability import DirectoryCapability, DirectoryCapabilityPool
from atlassian_skills.core.errors import ConflictError, ValidationError


@dataclass(frozen=True)
class PublicationResult:
    path: Path
    rewritten: bool
    sha256: str


@dataclass(frozen=True)
class PublicationFile:
    destination: Path
    content: bytes
    kind: str


@dataclass(frozen=True)
class _UnchangedFile:
    capability: DirectoryCapability
    path: Path
    identity: str
    sha256: str


def _content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _unchanged_file(
    pools: DirectoryCapabilityPool,
    item: PublicationFile,
    expected_sha256: str,
) -> _UnchangedFile | None:
    parent = item.destination.expanduser().absolute().parent
    if not parent.is_dir():
        return None
    capability = pools.acquire(parent)
    leaf = item.destination.name
    if not capability.leaf_exists(leaf):
        return None
    info = capability.lstat_leaf(leaf)
    if not stat.S_ISREG(info.st_mode):
        raise ValidationError(
            "Managed publication destination is not a regular file",
            context={"reason": "output_conflict", "path": str(item.destination)},
        )
    actual_sha256 = capability.sha256(leaf)
    if actual_sha256 != expected_sha256:
        return None
    return _UnchangedFile(
        capability=capability,
        path=capability.path_for_leaf(leaf),
        identity=capability.file_identity(leaf),
        sha256=actual_sha256,
    )


def _verify_unchanged(item: _UnchangedFile) -> None:
    leaf = item.path.name
    item.capability.revalidate()
    actual_identity = item.capability.file_identity(leaf)
    actual_sha256 = item.capability.sha256(leaf)
    if actual_identity != item.identity or actual_sha256 != item.sha256:
        raise ConflictError(
            "Managed output changed during publication",
            context={
                "reason": "output_changed_during_publication",
                "path": str(item.path),
                "expected_file_identity": item.identity,
                "actual_file_identity": actual_identity,
            },
        )


def publish_managed_files(files: tuple[PublicationFile, ...]) -> tuple[PublicationResult, ...]:
    """Atomically publish a file set without creating global state or rewriting identical bytes."""

    destinations: set[Path] = set()
    normalized: list[tuple[PublicationFile, str]] = []
    for item in files:
        destination = item.destination.expanduser().absolute()
        if destination in destinations:
            raise ValidationError(
                "Duplicate managed publication destination",
                context={"reason": "duplicate_output", "path": str(destination)},
            )
        destinations.add(destination)
        normalized.append((PublicationFile(destination, item.content, item.kind), _content_sha256(item.content)))

    batch = AttachmentWriteBatch()
    unchanged: list[_UnchangedFile] = []
    results: list[PublicationResult] = []
    with DirectoryCapabilityPool() as pools:
        try:
            for item, expected_sha256 in normalized:
                existing = _unchanged_file(pools, item, expected_sha256)
                if existing is not None:
                    unchanged.append(existing)
                    results.append(PublicationResult(item.destination, False, f"sha256:{expected_sha256}"))
                    continue
                batch.add(item.destination, item.content)
                results.append(PublicationResult(item.destination, True, f"sha256:{expected_sha256}"))
            batch.commit()
            for unchanged_item in unchanged:
                _verify_unchanged(unchanged_item)
        except BaseException:
            batch.abort()
            raise
    return tuple(results)


def publish_managed_file(file: PublicationFile) -> PublicationResult:
    """Publish one managed file through the common state-free batch."""

    return publish_managed_files((file,))[0]


__all__ = ["PublicationFile", "PublicationResult", "publish_managed_file", "publish_managed_files"]
