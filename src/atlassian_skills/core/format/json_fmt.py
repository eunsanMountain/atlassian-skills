from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel


def format_json(data: Any) -> str:
    """Return a minified JSON string.

    - pydantic BaseModel → model_dump_json()
    - list of pydantic models → serialize each item
    - dict / list / scalar → json.dumps
    """
    if isinstance(data, BaseModel):
        return data.model_dump_json(exclude_none=True)
    if isinstance(data, list) and data and isinstance(data[0], BaseModel):
        items = [json.loads(item.model_dump_json(exclude_none=True)) for item in data]
        return json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
