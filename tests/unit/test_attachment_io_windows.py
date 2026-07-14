from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path

import pytest

from atlassian_skills.core.attachment_io import (
    AttachmentWriteBatch,
    AttachmentWriter,
    AttachmentWriterKind,
    find_git_bash,
    verify_compatible_attachment_writer,
)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows and the installed Git Bash")
def test_compatible_batch_remains_byte_exact_for_130_seconds(tmp_path: Path) -> None:
    bash = find_git_bash()
    assert bash is not None
    verify_compatible_attachment_writer(bash)

    directory = tmp_path / "path with spaces" / "한글 — (attachments)"
    directory.mkdir(parents=True)
    expected: dict[Path, str] = {}
    batch = AttachmentWriteBatch(AttachmentWriter(AttachmentWriterKind.COMPATIBLE, directory, bash))
    for index in range(20):
        destination = directory / f"payload 한글 ({index}).bin"
        content = bytes(range(256)) * (257 + index) + b"\x00\xfffinal"
        expected[destination] = hashlib.sha256(content).hexdigest()
        batch.add(destination, content)

    batch.commit()

    started = time.monotonic()
    for checkpoint in (0, 40, 130):
        remaining = checkpoint - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)
        manifest = "".join(f"{digest} *{path.as_posix()}\n" for path, digest in expected.items())
        subprocess.run(
            [str(bash), "-c", "exec sha256sum -c -"],
            input=manifest.encode("utf-8"),
            check=True,
            capture_output=True,
            timeout=300,
        )
        assert list(directory.glob(".atls-*.part")) == []
