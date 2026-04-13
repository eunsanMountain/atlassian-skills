from __future__ import annotations

from typing import Any


def format_raw(data: Any) -> str:
    """Byte-preserving raw passthrough (§15.3).

    For core commands (issue get, page get), the CLI should bypass this function
    and output response.text directly. This fallback handles other commands
    where byte-preserving is less critical.
    """
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        import json

        return json.dumps(data, ensure_ascii=False)
    if hasattr(data, "model_dump"):
        import json

        return json.dumps(data.model_dump(exclude_none=True), ensure_ascii=False)
    if isinstance(data, list):
        import json

        def _serialize(item: Any) -> Any:
            if hasattr(item, "model_dump"):
                return item.model_dump(exclude_none=True)
            return item

        return json.dumps([_serialize(i) for i in data], ensure_ascii=False)
    return str(data)
