from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from atlassian_skills.core.config import load_config
from atlassian_skills.core.directory_capability import DirectoryCapability, DirectoryCapabilityPool
from atlassian_skills.core.errors import AtlasError, ValidationError

_ATOMIC_DOWNLOAD_CREATE_ATTEMPTS = 10
_ATTACHMENT_WRITE_TIMEOUT_SECONDS = 300
_COMPATIBLE_CAPABILITY_TIMEOUT_SECONDS = 15
_ERROR_DETAIL_LIMIT = 500
_BIDI_CONTROL_CODEPOINTS = frozenset(
    {
        0x061C,
        0x200E,
        0x200F,
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
    }
)

_COMPATIBLE_CAPABILITY_SCRIPT = "exec perl -MDigest::SHA -e 'exit 0'"
_COMPATIBLE_BATCH_COMMAND = 'exec perl -e "$1"'
_COMPATIBLE_BATCH_PERL = r"""
use strict;
use warnings;
use Digest::SHA ();
binmode STDIN, ':raw';
local $/ = "\0";

sub field {
    my $value = <STDIN>;
    die "truncated manifest" unless defined $value;
    chomp $value;
    return $value;
}

sub digest_file {
    my ($path) = @_;
    open my $handle, '<:raw', $path or die "open $path: $!";
    my $sha = Digest::SHA->new(256);
    $sha->addfile($handle);
    close $handle or die "close $path: $!";
    return $sha->hexdigest;
}

my @items;
while (defined(my $stage = <STDIN>)) {
    chomp $stage;
    my $destination = field();
    my $backup = field();
    my $expected = field();
    my $before = field();
    push @items, [$stage, $destination, $backup, $expected, $before];
}

for my $item (@items) {
    my ($stage, undef, undef, $expected, undef) = @$item;
    die "staged hash mismatch: $stage" unless digest_file($stage) eq $expected;
}

for my $item (@items) {
    my ($stage, $destination, $backup, $expected, $before) = @$item;
    if ($before ne '') {
        die "destination missing: $destination" unless -f $destination && !-l $destination;
        die "destination changed: $destination" unless digest_file($destination) eq $before;
        die "backup exists: $backup" if -e $backup || -l $backup;
        rename($destination, $backup) or die "backup $destination: $!";
        die "backup changed: $backup" unless digest_file($backup) eq $before;
        link($stage, $destination) or die "publish $destination: $!";
        unlink($stage) or die "remove stage $stage: $!";
    } else {
        link($stage, $destination) or die "publish new $destination: $!";
        unlink($stage) or die "remove stage $stage: $!";
    }
    die "final hash mismatch: $destination" unless digest_file($destination) eq $expected;
}
""".strip()


class AttachmentWriterKind(str, Enum):
    NATIVE = "native"
    COMPATIBLE = "compatible"


@dataclass(frozen=True)
class AttachmentWriter:
    kind: AttachmentWriterKind
    directory: Path | None = None
    bash_path: Path | None = None
    requested_directory: Path | None = field(default=None, compare=False)


class AttachmentBatchState(str, Enum):
    STAGING = "staging"
    COMMITTED = "committed"
    ABORTED = "aborted"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass
class _BatchItem:
    capability: DirectoryCapability
    stage: Path
    destination: Path
    backup: Path | None
    expected_hash: str
    before_exists: bool
    before_hash: str | None
    before_identity: str | None
    published_identity: str | None = None


def _is_windows() -> bool:
    return os.name == "nt"


def escape_bidi_controls_for_display(value: str) -> str:
    """Make directional controls visible without changing remote identity values."""

    return "".join(
        f"\\u{ord(character):04x}" if ord(character) in _BIDI_CONTROL_CODEPOINTS else character for character in value
    )


def safe_attachment_filename(title: str, fallback_id: str) -> str:
    """Return a Windows-safe filename that cannot escape its output directory."""
    leaf = title.replace("\\", "/").rsplit("/", 1)[-1].lstrip(".")
    safe = "".join(
        "_"
        if character in '<>:"/\\|?*' or ord(character) < 32 or ord(character) in _BIDI_CONTROL_CODEPOINTS
        else character
        for character in leaf
    )
    safe = safe.rstrip(" .")
    if not safe:
        safe = f"attachment_{fallback_id}"

    stem = safe.split(".", 1)[0].casefold()
    if stem in {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}:
        safe = f"_{safe}"
    return safe[:180].rstrip(" .") or f"attachment_{fallback_id}"


def allocate_attachment_filename(title: str, fallback_id: str, used_names: set[str]) -> str:
    """Allocate a case-insensitively unique safe filename and update ``used_names``."""
    safe_name = safe_attachment_filename(title, fallback_id)
    original = Path(safe_name)
    suffix = original.suffix
    stem = original.stem
    duplicate_number = 2
    while safe_name.casefold() in used_names:
        safe_name = f"{stem} ({duplicate_number}){suffix}"
        duplicate_number += 1
    used_names.add(safe_name.casefold())
    return safe_name


def _existing_mode(capability: DirectoryCapability, leaf: str) -> int | None:
    if os.name != "posix":
        return None
    try:
        destination_stat = capability.lstat_leaf(leaf)
    except FileNotFoundError:
        return None
    return stat.S_IMODE(destination_stat.st_mode) if stat.S_ISREG(destination_stat.st_mode) else None


def _assert_destination_safe(capability: DirectoryCapability, leaf: str, destination: Path) -> None:
    try:
        destination_stat = capability.lstat_leaf(leaf)
    except FileNotFoundError:
        return
    file_attributes = int(getattr(destination_stat, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    if stat.S_ISLNK(destination_stat.st_mode) or file_attributes & reparse_flag:
        raise ValidationError(
            f"Attachment destination must not be a symlink or reparse point: {destination}",
            context={"reason": "unsafe_attachment_destination", "path": str(destination)},
        )
    if not stat.S_ISREG(destination_stat.st_mode):
        raise ValidationError(
            f"Attachment destination must be an ordinary file: {destination}",
            context={"reason": "unsafe_attachment_destination", "path": str(destination)},
        )
    if destination_stat.st_nlink != 1:
        raise ValidationError(
            f"Attachment destination must not have multiple hard links: {destination}",
            context={"reason": "unsafe_attachment_destination", "path": str(destination)},
        )


def _assert_item_destination_unchanged(item: _BatchItem) -> None:
    leaf = item.destination.name
    exists = item.capability.leaf_exists(leaf)
    if exists != item.before_exists:
        raise ValidationError(
            f"Attachment destination changed after staging: {item.destination}",
            context={"reason": "attachment_destination_changed", "path": str(item.destination)},
        )
    if not item.before_exists:
        return
    _assert_destination_safe(item.capability, leaf, item.destination)
    actual_identity = item.capability.file_identity(leaf)
    actual_hash = item.capability.sha256(leaf)
    if actual_identity != item.before_identity or actual_hash != item.before_hash:
        raise ValidationError(
            f"Attachment destination changed after staging: {item.destination}",
            context={
                "reason": "attachment_destination_changed",
                "path": str(item.destination),
                "expected_file_identity": item.before_identity,
                "actual_file_identity": actual_identity,
                "expected_sha256": item.before_hash,
                "actual_sha256": actual_hash,
            },
        )


def _unique_part_path(capability: DirectoryCapability, prefix: str) -> Path:
    return capability.path_for_leaf(f".{prefix}-{secrets.token_hex(16)}.part")


def _stage_bytes(capability: DirectoryCapability, destination: Path, content: bytes) -> Path:
    existing_mode = _existing_mode(capability, destination.name)
    for _ in range(_ATOMIC_DOWNLOAD_CREATE_ATTEMPTS):
        stage = _unique_part_path(capability, "atls-download")
        try:
            capability.write_bytes_exclusive(stage.name, content, mode=0o666)
        except FileExistsError:
            continue
        if existing_mode is not None:
            capability.chmod_leaf(stage.name, existing_mode)
        return stage
    raise FileExistsError(f"Unable to create a temporary download file for {destination}")


def _atomic_write_bytes(capability: DirectoryCapability, destination: Path, content: bytes) -> None:
    """Write bytes completely before atomically publishing the destination."""
    _assert_destination_safe(capability, destination.name, destination)
    stage = _stage_bytes(capability, destination, content)
    try:
        _assert_destination_safe(capability, destination.name, destination)
        capability.replace(stage.name, destination.name)
    except BaseException:
        capability.unlink(stage.name, missing_ok=True)
        raise


def atomic_write_bytes_bound(
    capability: DirectoryCapability,
    destination_leaf: str,
    content: bytes,
) -> Path:
    """Atomically publish bytes through an already acquired directory capability."""

    output = capability.path_for_leaf(destination_leaf)
    _atomic_write_bytes(capability, output, content)
    return output


def atomic_write_bytes(destination: str | Path, content: bytes) -> Path:
    """Atomically publish ordinary local bytes without resolving an attachment writer."""
    raw = Path(destination).expanduser()
    raw.parent.mkdir(parents=True, exist_ok=True)
    with DirectoryCapability.acquire(raw.parent) as capability:
        return atomic_write_bytes_bound(capability, raw.name, content)


def _git_bash_candidates() -> list[Path]:
    candidates: list[Path] = []
    for environment_name in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(environment_name)
        if root:
            candidates.extend(
                (Path(root) / "Git" / "bin" / "bash.exe", Path(root) / "Git" / "usr" / "bin" / "bash.exe")
            )
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.extend(
            (
                Path(local_app_data) / "Programs" / "Git" / "bin" / "bash.exe",
                Path(local_app_data) / "Programs" / "Git" / "usr" / "bin" / "bash.exe",
            )
        )
    discovered = shutil.which("bash")
    if discovered and (not _is_windows() or "git" in discovered.casefold()):
        candidates.append(Path(discovered))
    return candidates


def find_git_bash() -> Path | None:
    """Return the first available Git Bash executable as an absolute path."""
    seen: set[Path] = set()
    for candidate in _git_bash_candidates():
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    return None


def _subprocess_detail(exc: BaseException) -> str | None:
    detail: str | None = None
    if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
        detail = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr)
    elif isinstance(exc, subprocess.TimeoutExpired):
        detail = f"timed out after {exc.timeout} seconds"
    elif str(exc):
        detail = str(exc)
    return " ".join(detail.split())[:_ERROR_DETAIL_LIMIT] if detail else None


def verify_compatible_attachment_writer(bash_path: Path | None = None) -> Path:
    """Verify that Git Bash can start Perl with Digest::SHA."""
    selected = bash_path or find_git_bash()
    if selected is None:
        raise AtlasError(
            "Compatibility attachment writer is unavailable because Git Bash was not found",
            hint="Install Git for Windows or select the native attachment writer in atls setup",
        )
    resolved = selected.resolve()
    try:
        subprocess.run(
            [str(resolved), "-c", _COMPATIBLE_CAPABILITY_SCRIPT],
            check=True,
            capture_output=True,
            timeout=_COMPATIBLE_CAPABILITY_TIMEOUT_SECONDS,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = _subprocess_detail(exc)
        message = "Compatibility attachment writer requires Perl with Digest::SHA"
        if detail:
            message = f"{message}: {detail}"
        raise AtlasError(
            message,
            hint="Repair Git for Windows or select the native attachment writer in atls setup",
        ) from exc
    return resolved


def resolve_attachment_writer(directory: str | Path) -> AttachmentWriter:
    """Resolve the configured attachment writer without running a capability probe."""
    requested_directory = Path(directory).expanduser().absolute()
    resolved_directory = requested_directory.resolve()
    if not _is_windows() or load_config().attachment_writer == "native":
        return AttachmentWriter(
            AttachmentWriterKind.NATIVE,
            resolved_directory,
            requested_directory=requested_directory,
        )

    bash_path = find_git_bash()
    if bash_path is None:
        raise AtlasError(
            "Compatibility attachment writer is configured but Git Bash was not found",
            hint="Run atls setup and select the native writer, or install Git for Windows",
        )
    return AttachmentWriter(
        AttachmentWriterKind.COMPATIBLE,
        resolved_directory,
        bash_path.resolve(),
        requested_directory=requested_directory,
    )


class AttachmentWriteBatch:
    """Stage attachment bytes and publish the complete batch with one writer."""

    def __init__(self, writer: AttachmentWriter | None = None) -> None:
        self._writer = writer
        self._capabilities = DirectoryCapabilityPool()
        self._writer_requested_directory: Path | None = None
        self._writer_capability: DirectoryCapability | None = None
        if writer is not None and writer.directory is not None:
            requested = writer.requested_directory or writer.directory
            self._writer_requested_directory = requested.expanduser().absolute()
            self._writer_capability = self._capabilities.acquire(self._writer_requested_directory)
        self._items: list[_BatchItem] = []
        self._destinations: set[Path] = set()
        self.state = AttachmentBatchState.STAGING

    @property
    def writer(self) -> AttachmentWriter | None:
        return self._writer

    @property
    def destinations(self) -> tuple[Path, ...]:
        return tuple(item.destination for item in self._items)

    def bind_directory(self, directory: str | Path) -> DirectoryCapability:
        """Acquire and retain a destination capability before remote bytes are fetched."""

        requested = Path(directory).expanduser().absolute()
        requested.mkdir(parents=True, exist_ok=True)
        return self._capabilities.acquire(requested)

    def bind_child_directory(self, parent: DirectoryCapability, leaf: str) -> DirectoryCapability:
        """Create/open a direct child through an already-bound parent capability."""

        return self._capabilities.acquire_child(parent, leaf)

    def add(self, destination: str | Path, content: bytes) -> Path:
        if self.state is not AttachmentBatchState.STAGING:
            raise RuntimeError(f"Cannot add files to a {self.state.value} attachment batch")

        raw = Path(destination).expanduser()
        requested_parent = raw.parent.absolute()
        if (
            self._writer_capability is not None
            and self._writer_requested_directory is not None
            and os.path.normcase(str(requested_parent)) == os.path.normcase(str(self._writer_requested_directory))
        ):
            capability = self._writer_capability
        else:
            requested_parent.mkdir(parents=True, exist_ok=True)
            capability = self._capabilities.acquire(requested_parent)
        output = capability.path_for_leaf(raw.name)
        if output in self._destinations:
            raise ValidationError(f"Duplicate attachment destination in one batch: {output}")
        try:
            _assert_destination_safe(capability, output.name, output)
            before_exists = capability.leaf_exists(output.name)
            before_hash = capability.sha256(output.name) if before_exists else None
            before_identity = capability.file_identity(output.name) if before_exists else None
            if self._writer is None:
                self._writer = resolve_attachment_writer(capability.directory)

            stage = _stage_bytes(capability, output, content)
        except OSError as error:
            raise ValidationError(
                "Attachment publication preflight could not access its destination",
                context={
                    "reason": "attachment_preflight_io_failed",
                    "path": str(output),
                    "failure": type(error).__name__,
                },
            ) from error
        item = _BatchItem(
            capability=capability,
            stage=stage,
            destination=output,
            backup=(
                _unique_part_path(capability, "atls-backup")
                if self._writer.kind is AttachmentWriterKind.COMPATIBLE and before_exists
                else None
            ),
            expected_hash=hashlib.sha256(content).hexdigest(),
            before_exists=before_exists,
            before_hash=before_hash,
            before_identity=before_identity,
            published_identity=capability.file_identity(stage.name),
        )
        self._items.append(item)
        self._destinations.add(output)
        return output

    def commit(self) -> list[Path]:
        if self.state is not AttachmentBatchState.STAGING:
            raise RuntimeError(f"Cannot commit a {self.state.value} attachment batch")
        try:
            if not self._items:
                self.state = AttachmentBatchState.COMMITTED
                return []

            writer = self._writer
            assert writer is not None
            try:
                for item in self._items:
                    _assert_item_destination_unchanged(item)
            except BaseException:
                for item in self._items:
                    try:
                        item.capability.unlink(item.stage.name, missing_ok=True)
                    except OSError as error:
                        raise ValidationError(
                            "Attachment stage could not be removed after publication",
                            context={"reason": "attachment_cleanup_io_failed", "path": str(item.stage)},
                        ) from error
                self.state = AttachmentBatchState.ABORTED
                raise
            if writer.kind is AttachmentWriterKind.COMPATIBLE:
                self._commit_compatible(writer)
            else:
                self._commit_native()
            self.state = AttachmentBatchState.COMMITTED
            return list(self.destinations)
        finally:
            self._capabilities.close()

    def abort(self) -> None:
        if self.state is not AttachmentBatchState.STAGING:
            return
        try:
            if any(item.backup is not None and item.capability.leaf_exists(item.backup.name) for item in self._items):
                self.state = AttachmentBatchState.RECOVERY_REQUIRED
                return
            for item in self._items:
                item.capability.unlink(item.stage.name, missing_ok=True)
            self.state = AttachmentBatchState.ABORTED
        finally:
            self._capabilities.close()

    def _commit_native(self) -> None:
        for item in self._items:
            if item.before_exists:
                item.backup = _unique_part_path(item.capability, "atls-backup")
        active_destination: Path | None = None
        try:
            for item in self._items:
                active_destination = item.destination
                _assert_item_destination_unchanged(item)
                if item.before_exists:
                    assert item.backup is not None
                    try:
                        item.capability.promote_no_replace(item.destination.name, item.backup.name)
                    except FileExistsError as error:
                        raise ValidationError(
                            f"Attachment backup path already exists: {item.backup}",
                            context={"reason": "attachment_backup_collision", "path": str(item.backup)},
                        ) from error
                    backup_identity = item.capability.file_identity(item.backup.name)
                    if (
                        backup_identity != item.before_identity
                        or item.capability.sha256(item.backup.name) != item.before_hash
                    ):
                        raise ValidationError(
                            f"Attachment backup changed during publication: {item.destination}",
                            context={
                                "reason": "attachment_destination_changed",
                                "path": str(item.destination),
                                "expected_file_identity": item.before_identity,
                                "actual_file_identity": backup_identity,
                            },
                        )
                try:
                    item.capability.promote_no_replace(item.stage.name, item.destination.name)
                except FileExistsError as error:
                    raise ValidationError(
                        f"Attachment destination appeared during publication: {item.destination}",
                        context={"reason": "attachment_destination_changed", "path": str(item.destination)},
                    ) from error
                item.published_identity = item.capability.file_identity(item.destination.name)
                if item.capability.sha256(item.destination.name) != item.expected_hash:
                    raise OSError(f"Final attachment hash mismatch: {item.destination}")
        except BaseException as error:
            recovery_errors = self._recover()
            self.state = AttachmentBatchState.ABORTED
            if recovery_errors:
                raise AtlasError(
                    f"Native attachment batch failed and recovery was incomplete: {'; '.join(recovery_errors)}",
                    hint="Do not remove remaining .atls-backup files until their original destinations are restored",
                ) from None
            if isinstance(error, (KeyboardInterrupt, SystemExit, AtlasError)):
                raise
            raise ValidationError(
                f"Native attachment publication failed for {active_destination}",
                context={"reason": "attachment_publication_io_failed", "failure": type(error).__name__},
            ) from error
        try:
            self._discard_backups()
        except BaseException:
            self.state = AttachmentBatchState.RECOVERY_REQUIRED
            raise

    def _commit_compatible(self, writer: AttachmentWriter) -> None:
        if writer.bash_path is None:
            self._recover()
            self.state = AttachmentBatchState.ABORTED
            raise AtlasError(
                "Compatibility attachment writer has no Git Bash executable",
                hint="Run atls setup and select an available attachment writer",
            )

        for item in self._items:
            item.capability.revalidate()
        manifest = b"".join(
            field.encode("utf-8") + b"\0"
            for item in self._items
            for field in (
                item.stage.as_posix(),
                item.destination.as_posix(),
                item.backup.as_posix() if item.backup is not None else "",
                item.expected_hash,
                item.before_hash or "",
            )
        )
        try:
            subprocess.run(
                [str(writer.bash_path), "-c", _COMPATIBLE_BATCH_COMMAND, "atls", _COMPATIBLE_BATCH_PERL],
                input=manifest,
                check=True,
                capture_output=True,
                timeout=_ATTACHMENT_WRITE_TIMEOUT_SECONDS,
                shell=False,
            )
            for item in self._items:
                item.capability.revalidate()
                item.published_identity = item.capability.file_identity(item.destination.name)
                if not item.before_exists:
                    continue
                assert item.backup is not None
                backup_identity = item.capability.file_identity(item.backup.name)
                if (
                    backup_identity != item.before_identity
                    or item.capability.sha256(item.backup.name) != item.before_hash
                ):
                    raise ValidationError(
                        f"Attachment backup changed during compatibility publication: {item.destination}",
                        context={
                            "reason": "attachment_destination_changed",
                            "path": str(item.destination),
                            "expected_file_identity": item.before_identity,
                            "actual_file_identity": backup_identity,
                        },
                    )
        except BaseException as exc:
            recovery_errors = self._recover()
            self.state = AttachmentBatchState.ABORTED
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            detail = _subprocess_detail(exc)
            message = "Compatibility attachment batch failed byte-integrity verification"
            if detail:
                message = f"{message}: {detail}"
            hint = (
                "The previous files were restored. Check Git Bash and Perl, then retry or select native in atls setup"
            )
            if recovery_errors:
                message = f"{message}; recovery incomplete: {'; '.join(recovery_errors)}"
                hint = "Keep remaining .atls-backup files and restore them before retrying"
            raise AtlasError(
                message,
                hint=hint,
            ) from exc
        try:
            self._discard_backups()
        except BaseException:
            self.state = AttachmentBatchState.RECOVERY_REQUIRED
            raise

    def _recover(self) -> list[str]:
        errors: list[str] = []
        for item in reversed(self._items):
            if item.backup is not None and item.capability.leaf_exists(item.backup.name):
                if item.capability.leaf_exists(item.destination.name):
                    try:
                        if (
                            item.published_identity is None
                            or item.capability.file_identity(item.destination.name) != item.published_identity
                            or item.capability.sha256(item.destination.name) != item.expected_hash
                        ):
                            errors.append(f"preserve unrelated {item.destination}")
                            continue
                        item.capability.unlink(item.destination.name)
                    except OSError as exc:
                        errors.append(f"remove {item.destination}: {exc}")
                        continue
                try:
                    try:
                        item.capability.promote_no_replace(item.backup.name, item.destination.name)
                    except FileExistsError as exc:
                        errors.append(f"preserve concurrent {item.destination}: {exc}")
                except OSError as exc:
                    errors.append(f"restore {item.destination} from {item.backup}: {exc}")
            elif not item.before_exists and item.capability.leaf_exists(item.destination.name):
                try:
                    if (
                        item.published_identity is not None
                        and item.capability.file_identity(item.destination.name) == item.published_identity
                        and item.capability.sha256(item.destination.name) == item.expected_hash
                    ):
                        item.capability.unlink(item.destination.name)
                except OSError as exc:
                    errors.append(f"remove new {item.destination}: {exc}")
            try:
                item.capability.unlink(item.stage.name, missing_ok=True)
            except OSError as exc:
                errors.append(f"remove stage {item.stage}: {exc}")
        return errors

    def _discard_backups(self) -> None:
        for item in self._items:
            if (
                item.published_identity is None
                or item.capability.file_identity(item.destination.name) != item.published_identity
                or item.capability.sha256(item.destination.name) != item.expected_hash
            ):
                raise ValidationError(
                    "Attachment destination changed before backup cleanup",
                    context={"reason": "attachment_cleanup_conflict", "path": str(item.destination)},
                )
        for item in self._items:
            if item.capability.leaf_exists(item.stage.name):
                if (
                    item.capability.file_identity(item.stage.name) != item.published_identity
                    or item.capability.sha256(item.stage.name) != item.expected_hash
                ):
                    raise ValidationError(
                        "Attachment stage changed before cleanup",
                        context={"reason": "attachment_cleanup_conflict", "path": str(item.stage)},
                    )
                item.capability.unlink(item.stage.name, missing_ok=True)
            if item.backup is not None:
                if item.capability.leaf_exists(item.backup.name):
                    if (
                        item.capability.file_identity(item.backup.name) != item.before_identity
                        or item.capability.sha256(item.backup.name) != item.before_hash
                    ):
                        raise ValidationError(
                            "Attachment backup changed before cleanup",
                            context={"reason": "attachment_cleanup_conflict", "path": str(item.backup)},
                        )
                    try:
                        item.capability.unlink(item.backup.name)
                    except OSError as error:
                        raise ValidationError(
                            "Attachment backup could not be removed after publication",
                            context={"reason": "attachment_cleanup_io_failed", "path": str(item.backup)},
                        ) from error


@contextmanager
def open_verified_attachment_snapshot(
    path: Path,
    *,
    managed_root: Path,
    expected_identity: str,
    expected_sha256: str,
    reference: str,
) -> Iterator[BinaryIO]:
    """Yield a snapshot opened through no-follow directory capabilities."""

    try:
        relative = PurePosixPath(reference)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValidationError(
                "Managed attachment reference is not portable",
                context={"reason": "asset_changed_after_preflight", "src": reference},
            )
        expected_path = managed_root.joinpath(*relative.parts).absolute()
        if path.absolute() != expected_path:
            raise ValidationError(
                "Managed attachment path no longer matches its portable reference",
                context={"reason": "asset_changed_after_preflight", "src": reference},
            )
        root_info = managed_root.lstat()
        root_attributes = int(getattr(root_info, "st_file_attributes", 0))
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode) or root_attributes & reparse_flag:
            raise ValidationError(
                "Managed attachment root changed after preflight",
                context={"reason": "asset_changed_after_preflight", "src": reference},
            )
        with ExitStack() as capabilities:
            current = capabilities.enter_context(DirectoryCapability.acquire(managed_root))
            for part in relative.parts[:-1]:
                current = capabilities.enter_context(current.acquire_child_directory(part, create=False))
            stream = capabilities.enter_context(
                current.open_readonly(
                    relative.name,
                    expected_identity=expected_identity,
                    expected_sha256=expected_sha256.removeprefix("sha256:"),
                )
            )
            with tempfile.TemporaryFile(mode="w+b") as snapshot:
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                    snapshot.write(chunk)
                if digest.hexdigest() != expected_sha256.removeprefix("sha256:"):
                    raise ValidationError(
                        "Managed attachment changed after preflight",
                        context={"reason": "asset_changed_after_preflight", "src": reference},
                    )
                snapshot.flush()
                snapshot.seek(0)
                yield snapshot
    except ValidationError as error:
        if (error.context or {}).get("reason") == "asset_changed_after_preflight":
            raise
        raise ValidationError(
            "Managed attachment path changed after preflight",
            context={
                "reason": "asset_changed_after_preflight",
                "src": reference,
                "failure": str((error.context or {}).get("reason", type(error).__name__)),
            },
        ) from error
    except OSError as error:
        raise ValidationError(
            "Managed attachment could not be reopened safely for upload",
            context={
                "reason": "asset_changed_after_preflight",
                "src": reference,
                "failure": type(error).__name__,
            },
        ) from error


def write_attachments_batch(
    attachments: Iterable[tuple[str | Path, bytes]],
    *,
    writer: AttachmentWriter | None = None,
) -> list[Path]:
    """Stage and publish multiple attachments using one batch writer."""
    batch = AttachmentWriteBatch(writer)
    try:
        for destination, content in attachments:
            batch.add(destination, content)
        return batch.commit()
    except BaseException:
        batch.abort()
        raise


def write_attachment_bytes(
    destination: str | Path,
    content: bytes,
    *,
    writer: AttachmentWriter | None = None,
) -> Path:
    """Write one attachment through the shared batch implementation."""
    return write_attachments_batch([(destination, content)], writer=writer)[0]
