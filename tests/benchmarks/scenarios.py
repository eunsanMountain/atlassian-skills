"""Token benchmark scenarios and helpers."""

from __future__ import annotations

import json
from pathlib import Path

import tiktoken

FIXTURES = Path(__file__).parent.parent / "fixtures"
ENCODING = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(ENCODING.encode(text))


def load_fixture(path: str) -> dict | list:  # type: ignore[type-arg]
    return json.loads((FIXTURES / path).read_text())
