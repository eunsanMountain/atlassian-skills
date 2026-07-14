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
    find_git_bash,
    resolve_attachment_writer,
    safe_attachment_filename,
    write_attachment_bytes,
    write_attachments_batch,
)
from atlassian_skills.core.config import Config
from atlassian_skills.core.errors import AtlasError, ValidationError


def _emulate_compatible_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
    manifest = kwargs["input"]
    assert isinstance(manifest, bytes)
    fields = manifest.split(b"\0")
    assert fields.pop() == b""
    assert len(fields) % 4 == 0
    for offset in range(0, len(fields), 4):
        stage, destination, backup, expected = (Path(value.decode("utf-8")) for value in fields[offset : offset + 4])
        assert hashlib.sha256(stage.read_bytes()).hexdigest() == str(expected)
        if destination.exists():
            os.replace(destination, backup)
        os.replace(stage, destination)
        assert hashlib.sha256(destination.read_bytes()).hexdigest() == str(expected)
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
    real_replace = os.replace

    def fail_second_publish(source: Path, destination: Path) -> None:
        if source.name.startswith(".atls-download-") and destination == second.resolve():
            raise OSError("forced second publish failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_second_publish)

    with pytest.raises(OSError, match="forced second publish failure"):
        batch.commit()

    assert first.read_bytes() == b"old-first"
    assert second.read_bytes() == b"old-second"
    assert list(tmp_path.glob(".atls-*.part")) == []


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
    assert kwargs["input"].count(b"\0") == count * 4  # type: ignore[union-attr]
    assert kwargs["shell"] is False
    assert all(path.read_bytes() == content for path, content in expected.items())
    assert not any(str(path) in attachment_io._COMPATIBLE_BATCH_PERL for path in expected)
    assert list(tmp_path.rglob(".atls-*.part")) == []


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
        stage_value, destination_value, backup_value, _expected, *_rest = manifest.split(b"\0")
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
        stage_value, destination_value, backup_value, _expected, _end = manifest.split(b"\0")
        stage = Path(stage_value.decode())
        target = Path(destination_value.decode())
        backup = Path(backup_value.decode())
        real_replace(target, backup)
        real_replace(stage, target)

        def fail_restore(source: Path, restored: Path) -> None:
            if source == backup and restored == target:
                raise OSError("restore blocked")
            real_replace(source, restored)

        monkeypatch.setattr(os, "replace", fail_restore)
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
