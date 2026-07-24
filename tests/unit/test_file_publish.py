from __future__ import annotations

import ctypes
from pathlib import Path

import pytest

import atlassian_skills.core.file_publish as file_publish


class _RenameX:
    def __init__(self) -> None:
        self.argtypes: object = None
        self.restype: object = None
        self.calls: list[tuple[bytes, bytes, int]] = []

    def __call__(self, source: bytes, destination: bytes, flags: int) -> int:
        self.calls.append((source, destination, flags))
        return 0


def test_darwin_no_replace_uses_renamex_np_exclusive_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    renamex = _RenameX()
    libc = type("LibC", (), {"renamex_np": renamex})()
    monkeypatch.setattr(file_publish.sys, "platform", "darwin")
    monkeypatch.setattr(file_publish.ctypes, "CDLL", lambda *_args, **_kwargs: libc)
    source = tmp_path / "source"
    destination = tmp_path / "destination"

    file_publish.promote_file_no_replace(source, destination)

    assert renamex.calls == [(bytes(source), bytes(destination), 0x00000004)]
    assert renamex.argtypes == [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    assert renamex.restype is ctypes.c_int
