from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from atlassian_skills.core import attachment_io
from atlassian_skills.core.attachment_io import (
    AttachmentBatchState,
    AttachmentWriteBatch,
    AttachmentWriter,
    AttachmentWriterKind,
    atomic_write_bytes,
    escape_bidi_controls_for_display,
    find_git_bash,
    resolve_attachment_writer,
    safe_attachment_filename,
    write_attachment_bytes,
    write_attachments_batch,
)
from atlassian_skills.core.config import Config
from atlassian_skills.core.errors import AtlasError, ValidationError
from atlassian_skills.core.file_identity import inspect_file_identity


def _emulate_compatible_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
    manifest = kwargs["input"]
    assert isinstance(manifest, bytes)
    fields = manifest.split(b"\0")
    assert fields.pop() == b""
    assert len(fields) % 5 == 0
    for offset in range(0, len(fields), 5):
        stage_value, destination_value, backup_value, expected_value, before_value = fields[offset : offset + 5]
        stage = Path(stage_value.decode("utf-8"))
        destination = Path(destination_value.decode("utf-8"))
        backup = Path(backup_value.decode("utf-8"))
        expected = expected_value.decode("ascii")
        before = before_value.decode("ascii")
        assert hashlib.sha256(stage.read_bytes()).hexdigest() == expected
        if before:
            assert hashlib.sha256(destination.read_bytes()).hexdigest() == before
            os.replace(destination, backup)
        os.link(stage, destination)
        stage.unlink()
        assert hashlib.sha256(destination.read_bytes()).hexdigest() == expected
    return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")


def test_default_writer_is_native_on_windows_without_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(attachment_io, "_is_windows", lambda: True)
    monkeypatch.setattr(attachment_io, "load_config", lambda: Config())

    def unexpected_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("the native writer must not start a subprocess")

    monkeypatch.setattr(subprocess, "run", unexpected_run)

    writer = resolve_attachment_writer(tmp_path)

    assert writer == AttachmentWriter(kind=AttachmentWriterKind.NATIVE, directory=tmp_path.resolve())


def test_non_windows_ignores_transferred_compatible_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(attachment_io, "_is_windows", lambda: False)
    monkeypatch.setattr(attachment_io, "load_config", lambda: Config(attachment_writer="compatible"))

    assert resolve_attachment_writer(tmp_path).kind is AttachmentWriterKind.NATIVE


def test_configured_compatible_writer_resolves_without_capability_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bash = tmp_path / "bash.exe"
    bash.write_bytes(b"")
    monkeypatch.setattr(attachment_io, "_is_windows", lambda: True)
    monkeypatch.setattr(attachment_io, "load_config", lambda: Config(attachment_writer="compatible"))
    monkeypatch.setattr(attachment_io, "find_git_bash", lambda: bash)

    def unexpected_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("writer resolution must not run a capability subprocess")

    monkeypatch.setattr(subprocess, "run", unexpected_run)

    writer = resolve_attachment_writer(tmp_path)

    assert writer == AttachmentWriter(AttachmentWriterKind.COMPATIBLE, tmp_path.resolve(), bash.resolve())


def test_configured_compatible_writer_fails_closed_when_bash_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(attachment_io, "_is_windows", lambda: True)
    monkeypatch.setattr(attachment_io, "load_config", lambda: Config(attachment_writer="compatible"))
    monkeypatch.setattr(attachment_io, "find_git_bash", lambda: None)

    with pytest.raises(AtlasError, match="configured but Git Bash was not found"):
        resolve_attachment_writer(tmp_path)


def test_find_git_bash_prefers_well_known_installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bash = tmp_path / "Git" / "bin" / "bash.exe"
    bash.parent.mkdir(parents=True)
    bash.write_bytes(b"")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(attachment_io.shutil, "which", lambda _name: None)

    assert find_git_bash() == bash.resolve()


def test_native_batch_publishes_without_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destinations = [(tmp_path / f"asset-{index}.bin", f"value-{index}".encode()) for index in range(20)]

    def unexpected_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("the native batch must not start a subprocess")

    monkeypatch.setattr(subprocess, "run", unexpected_run)
    writer = AttachmentWriter(AttachmentWriterKind.NATIVE, tmp_path)

    written = write_attachments_batch(destinations, writer=writer)

    assert written == [path.resolve() for path, _ in destinations]
    assert [path.read_bytes() for path, _ in destinations] == [content for _, content in destinations]
    assert list(tmp_path.glob(".atls-*.part")) == []


@pytest.mark.parametrize("writer_kind", ["atomic", "batch"])
def test_attachment_write_rejects_final_symlink_without_touching_target(tmp_path: Path, writer_kind: str) -> None:
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"keep-me")
    destination = tmp_path / "asset.bin"
    destination.symlink_to(victim)

    with pytest.raises(ValidationError) as exc_info:
        if writer_kind == "atomic":
            atomic_write_bytes(destination, b"attacker-bytes")
        else:
            batch = AttachmentWriteBatch(AttachmentWriter(AttachmentWriterKind.NATIVE, tmp_path))
            batch.add(destination, b"attacker-bytes")

    assert exc_info.value.context["reason"] == "unsafe_attachment_destination"
    assert victim.read_bytes() == b"keep-me"
    assert destination.is_symlink()


def test_native_attachment_batch_does_not_clobber_file_created_after_staging(tmp_path: Path) -> None:
    destination = tmp_path / "asset.bin"
    batch = AttachmentWriteBatch(AttachmentWriter(AttachmentWriterKind.NATIVE, tmp_path))
    batch.add(destination, b"managed")
    destination.write_bytes(b"user-owned")

    with pytest.raises(ValidationError) as exc_info:
        batch.commit()

    assert exc_info.value.context["reason"] == "attachment_destination_changed"
    assert destination.read_bytes() == b"user-owned"


def test_atomic_write_fsyncs_parent_directory_after_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path] = []
    original = attachment_io.DirectoryCapability.fsync

    def observed_fsync(capability: attachment_io.DirectoryCapability) -> None:
        calls.append(capability.directory)
        original(capability)

    monkeypatch.setattr(attachment_io.DirectoryCapability, "fsync", observed_fsync)

    destination = atomic_write_bytes(tmp_path / "journal.md", b"durable")

    assert destination.read_bytes() == b"durable"
    assert calls == [tmp_path.resolve()]


def test_compatible_attachment_batch_rejects_collision_before_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "asset.bin"
    bash = (tmp_path / "Git Bash" / "bash.exe").resolve()
    batch = AttachmentWriteBatch(AttachmentWriter(AttachmentWriterKind.COMPATIBLE, tmp_path, bash))
    batch.add(destination, b"managed")
    destination.write_bytes(b"user-owned")
    subprocess_called = False

    def unexpected_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal subprocess_called
        subprocess_called = True
        raise AssertionError("compatible writer must not run after a failed destination preflight")

    monkeypatch.setattr(subprocess, "run", unexpected_run)

    with pytest.raises(ValidationError) as exc_info:
        batch.commit()

    assert exc_info.value.context["reason"] == "attachment_destination_changed"
    assert subprocess_called is False
    assert destination.read_bytes() == b"user-owned"


def test_native_attachment_batch_rejects_existing_file_replaced_after_staging(tmp_path: Path) -> None:
    destination = tmp_path / "asset.bin"
    destination.write_bytes(b"original")
    batch = AttachmentWriteBatch(AttachmentWriter(AttachmentWriterKind.NATIVE, tmp_path))
    batch.add(destination, b"managed")
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"concurrent")
    os.replace(replacement, destination)

    with pytest.raises(ValidationError) as exc_info:
        batch.commit()

    assert exc_info.value.context["reason"] == "attachment_destination_changed"
    assert destination.read_bytes() == b"concurrent"


def test_native_attachment_batch_restores_same_bytes_inode_swap_before_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "asset.bin"
    replacement = tmp_path / "replacement.bin"
    destination.write_bytes(b"old")
    replacement.write_bytes(b"old")
    batch = AttachmentWriteBatch(AttachmentWriter(AttachmentWriterKind.NATIVE, tmp_path))
    batch.add(destination, b"new")
    replacement_identity = inspect_file_identity(replacement).key
    real_replace = os.replace
    real_promote = attachment_io.DirectoryCapability.promote_no_replace
    swapped = False

    def swap_then_backup(
        capability: attachment_io.DirectoryCapability,
        source_leaf: str,
        target_leaf: str,
    ) -> None:
        nonlocal swapped
        if source_leaf == destination.name and not swapped:
            swapped = True
            real_replace(replacement, destination)
        real_promote(capability, source_leaf, target_leaf)

    monkeypatch.setattr(attachment_io.DirectoryCapability, "promote_no_replace", swap_then_backup)

    with pytest.raises(ValidationError):
        batch.commit()

    assert inspect_file_identity(destination).key == replacement_identity
    assert destination.read_bytes() == b"old"


def test_native_multi_file_failure_restores_previous_destinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"old-first")
    second.write_bytes(b"old-second")
    batch = AttachmentWriteBatch(AttachmentWriter(AttachmentWriterKind.NATIVE, tmp_path))
    batch.add(first, b"new-first")
    batch.add(second, b"new-second")
    real_promote = attachment_io.DirectoryCapability.promote_no_replace

    def fail_second_publish(
        capability: attachment_io.DirectoryCapability,
        source_leaf: str,
        destination_leaf: str,
    ) -> None:
        if source_leaf.startswith(".atls-download-") and destination_leaf == second.name:
            raise OSError("forced second publish failure")
        real_promote(capability, source_leaf, destination_leaf)

    monkeypatch.setattr(attachment_io.DirectoryCapability, "promote_no_replace", fail_second_publish)

    with pytest.raises(ValidationError) as exc_info:
        batch.commit()
    assert exc_info.value.context["reason"] == "attachment_publication_io_failed"

    assert first.read_bytes() == b"old-first"
    assert second.read_bytes() == b"old-second"
    assert list(tmp_path.glob(".atls-*.part")) == []


def test_native_cleanup_conflict_preserves_backup_through_outer_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "asset.bin"
    concurrent = tmp_path / "concurrent.bin"
    destination.write_bytes(b"old")
    concurrent.write_bytes(b"concurrent")
    batch = AttachmentWriteBatch(AttachmentWriter(AttachmentWriterKind.NATIVE, tmp_path))
    batch.add(destination, b"new")
    real_discard = batch._discard_backups

    def race_then_discard() -> None:
        os.replace(concurrent, destination)
        real_discard()

    monkeypatch.setattr(batch, "_discard_backups", race_then_discard)

    with pytest.raises(ValidationError) as exc_info:
        batch.commit()
    assert exc_info.value.context["reason"] == "attachment_cleanup_conflict"
    assert batch.state is AttachmentBatchState.RECOVERY_REQUIRED
    batch.abort()
    backups = list(tmp_path.glob(".atls-backup-*.part"))
    assert destination.read_bytes() == b"concurrent"
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"old"


@pytest.mark.parametrize("count", [1, 20, 300])
def test_compatible_batch_uses_one_nul_manifest_subprocess(
    count: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bash = (tmp_path / "Git Bash" / "bash.exe").resolve()
    writer = AttachmentWriter(AttachmentWriterKind.COMPATIBLE, tmp_path, bash)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def capture_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((args, kwargs))
        return _emulate_compatible_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", capture_run)
    batch = AttachmentWriteBatch(writer)
    expected: dict[Path, bytes] = {}
    for index in range(count):
        destination = tmp_path / "공백 경로 — (assets)" / f"파일 {index}.bin"
        content = f"content-{index}".encode()
        batch.add(destination, content)
        expected[destination.resolve()] = content

    assert batch.commit() == list(expected)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == [
        str(bash),
        "-c",
        attachment_io._COMPATIBLE_BATCH_COMMAND,
        "atls",
        attachment_io._COMPATIBLE_BATCH_PERL,
    ]
    assert kwargs["input"].count(b"\0") == count * 5  # type: ignore[union-attr]
    assert kwargs["shell"] is False
    assert all(path.read_bytes() == content for path, content in expected.items())
    assert not any(str(path) in attachment_io._COMPATIBLE_BATCH_PERL for path in expected)
    assert list(tmp_path.rglob(".atls-*.part")) == []


def test_compatible_attachment_batch_restores_same_bytes_inode_swap_before_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bash = (tmp_path / "Git Bash" / "bash.exe").resolve()
    destination = tmp_path / "asset.bin"
    replacement = tmp_path / "replacement.bin"
    destination.write_bytes(b"old")
    replacement.write_bytes(b"old")
    replacement_identity = inspect_file_identity(replacement).key

    def race_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        os.replace(replacement, destination)
        return _emulate_compatible_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", race_run)
    batch = AttachmentWriteBatch(AttachmentWriter(AttachmentWriterKind.COMPATIBLE, tmp_path, bash))
    batch.add(destination, b"new")

    with pytest.raises(AtlasError):
        batch.commit()

    assert inspect_file_identity(destination).key == replacement_identity
    assert destination.read_bytes() == b"old"


def test_empty_compatible_batch_uses_zero_subprocesses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected subprocess"))
    batch = AttachmentWriteBatch(AttachmentWriter(AttachmentWriterKind.COMPATIBLE, tmp_path, tmp_path / "bash"))

    assert batch.commit() == []
    assert batch.state is AttachmentBatchState.COMMITTED


@pytest.mark.parametrize("failure", ["process", "timeout", "interrupt"])
def test_compatible_partial_failure_restores_existing_destinations(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"old-first")
    second.write_bytes(b"old-second")
    batch = AttachmentWriteBatch(AttachmentWriter(AttachmentWriterKind.COMPATIBLE, tmp_path, tmp_path / "bash.exe"))
    batch.add(first, b"new-first")
    batch.add(second, b"new-second")

    def fail_after_one(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        manifest = kwargs["input"]
        assert isinstance(manifest, bytes)
        stage_value, destination_value, backup_value, _expected, _before, *_rest = manifest.split(b"\0")
        os.replace(Path(destination_value.decode()), Path(backup_value.decode()))
        os.replace(Path(stage_value.decode()), Path(destination_value.decode()))
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 300)
        if failure == "interrupt":
            raise KeyboardInterrupt
        raise subprocess.CalledProcessError(1, args, stderr=b"forced failure")

    monkeypatch.setattr(subprocess, "run", fail_after_one)

    error = KeyboardInterrupt if failure == "interrupt" else AtlasError
    with pytest.raises(error):
        batch.commit()

    assert first.read_bytes() == b"old-first"
    assert second.read_bytes() == b"old-second"
    assert batch.state is AttachmentBatchState.ABORTED
    assert list(tmp_path.glob(".atls-*.part")) == []


def test_compatible_failure_does_not_fall_back_to_native(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "result.bin"
    destination.write_bytes(b"old")
    writer = AttachmentWriter(AttachmentWriterKind.COMPATIBLE, tmp_path, tmp_path / "bash.exe")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["bash"], stderr=b"hash mismatch")
        ),
    )

    with pytest.raises(AtlasError, match="byte-integrity verification"):
        write_attachment_bytes(destination, b"replacement", writer=writer)

    assert destination.read_bytes() == b"old"


def test_recovery_failure_preserves_backup_for_manual_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "result.bin"
    destination.write_bytes(b"old")
    batch = AttachmentWriteBatch(AttachmentWriter(AttachmentWriterKind.COMPATIBLE, tmp_path, tmp_path / "bash.exe"))
    batch.add(destination, b"new")
    real_replace = os.replace

    def publish_then_fail(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        manifest = kwargs["input"]
        assert isinstance(manifest, bytes)
        stage_value, destination_value, backup_value, _expected, _before, _end = manifest.split(b"\0")
        stage = Path(stage_value.decode())
        target = Path(destination_value.decode())
        backup = Path(backup_value.decode())
        real_replace(target, backup)
        real_replace(stage, target)

        real_promote = attachment_io.DirectoryCapability.promote_no_replace

        def fail_restore(
            capability: attachment_io.DirectoryCapability,
            source_leaf: str,
            restored_leaf: str,
        ) -> None:
            if source_leaf == backup.name and restored_leaf == target.name:
                raise OSError("restore blocked")
            real_promote(capability, source_leaf, restored_leaf)

        monkeypatch.setattr(attachment_io.DirectoryCapability, "promote_no_replace", fail_restore)
        raise subprocess.CalledProcessError(1, args, stderr=b"forced failure")

    monkeypatch.setattr(subprocess, "run", publish_then_fail)

    with pytest.raises(AtlasError, match="recovery incomplete"):
        batch.commit()

    backups = list(tmp_path.glob(".atls-backup-*.part"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"old"


def test_duplicate_destination_aborts_and_cleans_stages(tmp_path: Path) -> None:
    destination = tmp_path / "duplicate.bin"

    with pytest.raises(ValidationError, match="Duplicate attachment destination"):
        write_attachments_batch(
            [(destination, b"first"), (destination, b"second")],
            writer=AttachmentWriter(AttachmentWriterKind.NATIVE, tmp_path),
        )

    assert not destination.exists()
    assert list(tmp_path.glob(".atls-*.part")) == []


def test_safe_attachment_filename_handles_traversal_windows_names_and_trailing_characters() -> None:
    assert safe_attachment_filename("../../.report.pdf", "12") == "report.pdf"
    assert safe_attachment_filename(r"..\..\CON.txt", "12") == "_CON.txt"
    assert safe_attachment_filename("bad:name?.png. ", "12") == "bad_name_.png"
    assert safe_attachment_filename("..", "12") == "attachment_12"


@pytest.mark.parametrize(
    "control",
    [
        "\u061c",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    ],
)
def test_safe_attachment_filename_neutralizes_bidi_controls(control: str) -> None:
    assert safe_attachment_filename(f"left{control}right.png", "12") == "left_right.png"


def test_bidi_controls_are_visible_in_human_output_without_changing_source_text() -> None:
    source = "invoice\u202egnp.exe"

    assert escape_bidi_controls_for_display(source) == r"invoice\u202egnp.exe"
    assert source == "invoice\u202egnp.exe"
