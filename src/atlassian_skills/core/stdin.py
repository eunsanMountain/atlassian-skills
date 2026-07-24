from __future__ import annotations

import sys
from pathlib import Path

from atlassian_skills.core.errors import ValidationError

MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MB


def _decode_body(data: bytes, source: str) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValidationError(f"{source} must be valid UTF-8") from error


def _strip_bom(content: str) -> str:
    return content.removeprefix("\ufeff")


def read_body(
    body: str | None = None,
    body_file: str | None = None,
) -> str:
    """Read body content from inline string, file, or stdin.

    Priority: body (inline) > body_file
    body_file="-" reads from stdin.
    """
    if body is not None:
        return _strip_bom(body)
    if body_file is not None:
        if body_file == "-":
            binary_stdin = getattr(sys.stdin, "buffer", None)
            if binary_stdin is not None:
                data = binary_stdin.read(MAX_BODY_SIZE + 1)
                if len(data) > MAX_BODY_SIZE:
                    raise ValidationError(f"Body input exceeds {MAX_BODY_SIZE // (1024 * 1024)}MB limit")
                return _decode_body(data, "Body input")
            content = sys.stdin.read(MAX_BODY_SIZE + 1)
            if len(content.encode("utf-8")) > MAX_BODY_SIZE:
                raise ValidationError(f"Body input exceeds {MAX_BODY_SIZE // (1024 * 1024)}MB limit")
            return _strip_bom(content)
        path = Path(body_file)
        if path.stat().st_size > MAX_BODY_SIZE:
            raise ValidationError(f"Body file exceeds {MAX_BODY_SIZE // (1024 * 1024)}MB limit")
        return _decode_body(path.read_bytes(), f"Body file {path}")
    raise ValidationError("Either --body or --body-file is required")
