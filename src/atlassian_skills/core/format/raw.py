from __future__ import annotations

from typing import Any


def format_raw(data: Any) -> str:
    """Byte-preserving raw passthrough (§15.3).

    - bytes → decoded as utf-8 (errors='replace') without any transformation
    - str → returned verbatim, no escaping or conversion
    - anything else → str()
    """
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    if isinstance(data, str):
        return data
    return str(data)
