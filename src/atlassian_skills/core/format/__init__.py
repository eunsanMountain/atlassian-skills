from __future__ import annotations

from enum import Enum
from typing import Any


class OutputFormat(str, Enum):
    COMPACT = "compact"
    JSON = "json"
    RAW = "raw"
    MD = "md"


def format_output(data: Any, fmt: OutputFormat) -> str:
    """Dispatch data to the appropriate formatter and return a string."""
    if fmt == OutputFormat.COMPACT:
        from atlassian_skills.core.format.compact import format_compact

        return format_compact(data)
    if fmt == OutputFormat.JSON:
        from atlassian_skills.core.format.json_fmt import format_json

        return format_json(data)
    if fmt == OutputFormat.RAW:
        from atlassian_skills.core.format.raw import format_raw

        return format_raw(data)
    if fmt == OutputFormat.MD:
        # markdown format on a plain dict/model — render as JSON fallback
        from atlassian_skills.core.format.json_fmt import format_json

        return format_json(data)
    # unreachable but satisfies type checker
    raise ValueError(f"Unknown format: {fmt}")


__all__ = ["OutputFormat", "format_output"]
