from __future__ import annotations

import json
from pathlib import Path

from atlassian_skills.core.format.raw import format_raw

FIXTURES = Path(__file__).parent / "fixtures"


def test_raw_preserves_jira_special_chars() -> None:
    """--format=raw must preserve ~ + - * {} [] without any escape/unescape."""
    # Test cases from PROJ failure: "2~3 스프린트" was dropped by MCP
    test_cases = [
        "2~3 스프린트",
        "test+value",
        "*bold* text",
        "{code}block{code}",
        "[link|http://example.com]",
        "a-b-c",
    ]
    for text in test_cases:
        result = format_raw(text)
        assert result == text, f"Raw format altered '{text}' → '{result}'"


def test_raw_preserves_fixture_description() -> None:
    """Raw format preserves fixture description byte-identical."""
    fixture = json.loads((FIXTURES / "jira/get-issue-proj3.json").read_text())
    desc = fixture.get("description", "")
    if desc:
        result = format_raw(desc)
        assert result == desc, "Raw format altered fixture description"


def test_raw_preserves_confluence_body() -> None:
    """Raw format preserves Confluence storage body."""
    fixture_path = FIXTURES / "confluence/get-page-sample-raw.json"
    if fixture_path.exists():
        fixture = json.loads(fixture_path.read_text())
        body = fixture.get("body", {}).get("storage", {}).get("value", "")
        if body:
            result = format_raw(body)
            assert result == body
