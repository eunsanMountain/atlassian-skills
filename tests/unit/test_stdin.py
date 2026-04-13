from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest

from atlassian_skills.core.errors import ValidationError
from atlassian_skills.core.stdin import read_body


class TestReadBody:
    def test_read_body_inline(self) -> None:
        result = read_body(body="hello")
        assert result == "hello"

    def test_read_body_file(self, tmp_path: Path) -> None:
        f = tmp_path / "body.txt"
        f.write_text("file content", encoding="utf-8")
        result = read_body(body_file=str(f))
        assert result == "file content"

    def test_read_body_stdin(self) -> None:
        fake_stdin = io.StringIO("stdin content")
        with patch("atlassian_skills.core.stdin.sys.stdin", fake_stdin):
            result = read_body(body_file="-")
        assert result == "stdin content"

    def test_read_body_missing(self) -> None:
        with pytest.raises(ValidationError, match="Either --body or --body-file is required"):
            read_body()

    def test_read_body_priority(self, tmp_path: Path) -> None:
        f = tmp_path / "body.txt"
        f.write_text("file content", encoding="utf-8")
        result = read_body(body="inline wins", body_file=str(f))
        assert result == "inline wins"
