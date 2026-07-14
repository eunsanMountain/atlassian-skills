from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import stat
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from atlassian_skills.core.config import load_config
from atlassian_skills.core.errors import AtlasError, ValidationError

_ATOMIC_DOWNLOAD_CREATE_ATTEMPTS = 10
_ATTACHMENT_WRITE_TIMEOUT_SECONDS = 300
_COMPATIBLE_CAPABILITY_TIMEOUT_SECONDS = 15
_ERROR_DETAIL_LIMIT = 500

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
    push @items, [$stage, $destination, $backup, $expected];
}

for my $item (@items) {
    my ($stage, undef, undef, $expected) = @$item;
    die "staged hash mismatch: $stage" unless digest_file($stage) eq $expected;
}

for my $item (@items) {
    my ($stage, $destination, $backup, $expected) = @$item;
    if (-e $destination || -l $destination) {
        rename($destination, $backup) or die "backup $destination: $!";
    }
    rename($stage, $destination) or die "publish $destination: $!";
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


class AttachmentBatchState(str, Enum):
    STAGING = "staging"
    COMMITTED = "committed"
    ABORTED = "aborted"


@dataclass
class _BatchItem:
    stage: Path
    destination: Path
    backup: Path | None
    expected_hash: str


def _is_windows() -> bool:
    return os.name == "nt"


def safe_attachment_filename(title: str, fallback_id: str) -> str:
    """Return a Windows-safe filename that cannot escape its output directory."""
    leaf = title.replace("\\", "/").rsplit("/", 1)[-1].lstrip(".")
    safe = "".join("_" if character in '<>:"/\\|?*' or ord(character) < 32 else character for character in leaf)
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


def _existing_mode(destination: Path) -> int | None:
    if os.name != "posix":
        return None
    try:
        destination_stat = destination.lstat()
    except FileNotFoundError:
        return None
    return stat.S_IMODE(destination_stat.st_mode) if stat.S_ISREG(destination_stat.st_mode) else None


def _unique_part_path(directory: Path, prefix: str) -> Path:
    return directory / f".{prefix}-{secrets.token_hex(16)}.part"


def _stage_bytes(destination: Path, content: bytes) -> Path:
    existing_mode = _existing_mode(destination)
    for _ in range(_ATOMIC_DOWNLOAD_CREATE_ATTEMPTS):
        stage = _unique_part_path(destination.parent, "atls-download")
        try:
            handle = stage.open("xb")
        except FileExistsError:
            continue
        try:
            with handle:
                handle.write(content)
            if existing_mode is not None:
                stage.chmod(existing_mode)
            return stage
        except BaseException:
            _safe_unlink(stage)
            raise
    raise FileExistsError(f"Unable to create a temporary download file for {destination}")


def _atomic_write_bytes(destination: Path, content: bytes) -> None:
    """Write bytes completely before atomically publishing the destination."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = _stage_bytes(destination, content)
    try:
        os.replace(stage, destination)
    except BaseException:
        _safe_unlink(stage)
        raise


def atomic_write_bytes(destination: str | Path, content: bytes) -> Path:
    """Atomically publish ordinary local bytes without resolving an attachment writer."""
    output = Path(destination).resolve()
    _atomic_write_bytes(output, content)
    return output


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
    resolved_directory = Path(directory).resolve()
    if not _is_windows() or load_config().attachment_writer == "native":
        return AttachmentWriter(AttachmentWriterKind.NATIVE, resolved_directory)

    bash_path = find_git_bash()
    if bash_path is None:
        raise AtlasError(
            "Compatibility attachment writer is configured but Git Bash was not found",
            hint="Run atls setup and select the native writer, or install Git for Windows",
        )
    return AttachmentWriter(AttachmentWriterKind.COMPATIBLE, resolved_directory, bash_path.resolve())


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


class AttachmentWriteBatch:
    """Stage attachment bytes and publish the complete batch with one writer."""

    def __init__(self, writer: AttachmentWriter | None = None) -> None:
        self._writer = writer
        self._items: list[_BatchItem] = []
        self._destinations: set[Path] = set()
        self.state = AttachmentBatchState.STAGING

    @property
    def writer(self) -> AttachmentWriter | None:
        return self._writer

    @property
    def destinations(self) -> tuple[Path, ...]:
        return tuple(item.destination for item in self._items)

    def add(self, destination: str | Path, content: bytes) -> Path:
        if self.state is not AttachmentBatchState.STAGING:
            raise RuntimeError(f"Cannot add files to a {self.state.value} attachment batch")

        output = Path(destination).resolve()
        if output in self._destinations:
            raise ValidationError(f"Duplicate attachment destination in one batch: {output}")
        if output.is_dir():
            raise ValidationError(f"Attachment destination is a directory: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        if self._writer is None:
            self._writer = resolve_attachment_writer(output.parent)

        stage = _stage_bytes(output, content)
        item = _BatchItem(
            stage=stage,
            destination=output,
            backup=(
                _unique_part_path(output.parent, "atls-backup")
                if self._writer.kind is AttachmentWriterKind.COMPATIBLE
                else None
            ),
            expected_hash=hashlib.sha256(content).hexdigest(),
        )
        self._items.append(item)
        self._destinations.add(output)
        return output

    def commit(self) -> list[Path]:
        if self.state is not AttachmentBatchState.STAGING:
            raise RuntimeError(f"Cannot commit a {self.state.value} attachment batch")
        if not self._items:
            self.state = AttachmentBatchState.COMMITTED
            return []

        writer = self._writer
        assert writer is not None
        if writer.kind is AttachmentWriterKind.COMPATIBLE:
            self._commit_compatible(writer)
        else:
            self._commit_native()
        self.state = AttachmentBatchState.COMMITTED
        return list(self.destinations)

    def abort(self) -> None:
        if self.state is not AttachmentBatchState.STAGING:
            return
        for item in self._items:
            _safe_unlink(item.stage)
            if item.backup is not None:
                _safe_unlink(item.backup)
        self.state = AttachmentBatchState.ABORTED

    def _commit_native(self) -> None:
        if len(self._items) == 1:
            item = self._items[0]
            try:
                os.replace(item.stage, item.destination)
            except BaseException:
                _safe_unlink(item.stage)
                self.state = AttachmentBatchState.ABORTED
                raise
            return

        for item in self._items:
            item.backup = _unique_part_path(item.destination.parent, "atls-backup")
        try:
            for item in self._items:
                assert item.backup is not None
                if item.destination.exists() or item.destination.is_symlink():
                    os.replace(item.destination, item.backup)
                os.replace(item.stage, item.destination)
                if _file_sha256(item.destination) != item.expected_hash:
                    raise OSError(f"Final attachment hash mismatch: {item.destination}")
        except BaseException:
            recovery_errors = self._recover()
            self.state = AttachmentBatchState.ABORTED
            if recovery_errors:
                raise AtlasError(
                    f"Native attachment batch failed and recovery was incomplete: {'; '.join(recovery_errors)}",
                    hint="Do not remove remaining .atls-backup files until their original destinations are restored",
                ) from None
            raise
        self._discard_backups()

    def _commit_compatible(self, writer: AttachmentWriter) -> None:
        if writer.bash_path is None:
            self._recover()
            self.state = AttachmentBatchState.ABORTED
            raise AtlasError(
                "Compatibility attachment writer has no Git Bash executable",
                hint="Run atls setup and select an available attachment writer",
            )

        manifest = b"".join(
            field.encode("utf-8") + b"\0"
            for item in self._items
            for field in (
                item.stage.as_posix(),
                item.destination.as_posix(),
                item.backup.as_posix() if item.backup is not None else "",
                item.expected_hash,
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
        self._discard_backups()

    def _recover(self) -> list[str]:
        errors: list[str] = []
        for item in reversed(self._items):
            if item.backup is not None and (item.backup.exists() or item.backup.is_symlink()):
                try:
                    item.destination.unlink(missing_ok=True)
                except OSError as exc:
                    errors.append(f"remove {item.destination}: {exc}")
                    continue
                try:
                    os.replace(item.backup, item.destination)
                except OSError as exc:
                    errors.append(f"restore {item.destination} from {item.backup}: {exc}")
            elif not item.stage.exists():
                try:
                    item.destination.unlink(missing_ok=True)
                except OSError as exc:
                    errors.append(f"remove new {item.destination}: {exc}")
            try:
                item.stage.unlink(missing_ok=True)
            except OSError as exc:
                errors.append(f"remove stage {item.stage}: {exc}")
        return errors

    def _discard_backups(self) -> None:
        for item in self._items:
            _safe_unlink(item.stage)
            if item.backup is not None:
                _safe_unlink(item.backup)


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
