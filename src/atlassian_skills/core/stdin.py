from __future__ import annotations

import sys
from pathlib import Path

from atlassian_skills.core.errors import ValidationError

MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MB


def read_body(
    body: str | None = None,
    body_file: str | None = None,
) -> str:
    """Read body content from inline string, file, or stdin.

    Priority: body (inline) > body_file
    body_file="-" reads from stdin.
    """
    if body is not None:
        return body
    if body_file is not None:
        if body_file == "-":
            content = sys.stdin.read(MAX_BODY_SIZE + 1)
            if len(content) > MAX_BODY_SIZE:
                raise ValidationError(f"Body input exceeds {MAX_BODY_SIZE // (1024 * 1024)}MB limit")
            return content
        path = Path(body_file)
        if path.stat().st_size > MAX_BODY_SIZE:
            raise ValidationError(f"Body file exceeds {MAX_BODY_SIZE // (1024 * 1024)}MB limit")
        return path.read_text(encoding="utf-8")
    raise ValidationError("Either --body or --body-file is required")
