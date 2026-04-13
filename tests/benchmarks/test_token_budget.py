"""L1 token budget benchmarks: compact output must use >=50% fewer tokens than MCP JSON."""
from __future__ import annotations

import json

import pytest

from atlassian_skills.confluence.models import ConfluenceSearchResult, Page
from atlassian_skills.core.format.compact import format_compact
from atlassian_skills.jira.models import Issue, SearchResult

from .scenarios import count_tokens, load_fixture

pytestmark = pytest.mark.benchmark


def test_scenario_1_jira_issue_get() -> None:
    fixture = load_fixture("jira/get-issue-proj3.json")
    mcp_tokens = count_tokens(json.dumps(fixture, ensure_ascii=False))

    issue = Issue.model_validate(fixture)
    compact_output = format_compact(issue)
    compact_tokens = count_tokens(compact_output)

    print(f"\nScenario 1 - Jira Issue GET: mcp={mcp_tokens} compact={compact_tokens} ratio={compact_tokens/mcp_tokens:.2%}")
    assert compact_tokens < mcp_tokens * 0.5, (
        f"L1 fail: compact={compact_tokens} >= threshold={mcp_tokens * 0.5:.0f} (mcp={mcp_tokens})"
    )


def test_scenario_2_jira_search() -> None:
    fixture = load_fixture("jira/search-proj.json")
    mcp_tokens = count_tokens(json.dumps(fixture, ensure_ascii=False))

    result = SearchResult.model_validate(fixture)
    compact_output = format_compact(result)
    compact_tokens = count_tokens(compact_output)

    print(f"\nScenario 2 - Jira Search: mcp={mcp_tokens} compact={compact_tokens} ratio={compact_tokens/mcp_tokens:.2%}")
    assert compact_tokens < mcp_tokens * 0.5, (
        f"L1 fail: compact={compact_tokens} >= threshold={mcp_tokens * 0.5:.0f} (mcp={mcp_tokens})"
    )


def test_scenario_3_confluence_page_get() -> None:
    # Fixture has a {"metadata": {...}} wrapper — unwrap to get the page dict
    raw = load_fixture("confluence/get-page-sample.json")
    assert isinstance(raw, dict)
    page_data = raw.get("metadata", raw)

    mcp_tokens = count_tokens(json.dumps(raw, ensure_ascii=False))

    page = Page.model_validate(page_data)
    compact_output = format_compact(page)
    compact_tokens = count_tokens(compact_output)

    print(f"\nScenario 3 - Confluence Page GET: mcp={mcp_tokens} compact={compact_tokens} ratio={compact_tokens/mcp_tokens:.2%}")
    assert compact_tokens < mcp_tokens * 0.5, (
        f"L1 fail: compact={compact_tokens} >= threshold={mcp_tokens * 0.5:.0f} (mcp={mcp_tokens})"
    )


def test_scenario_4_confluence_search() -> None:
    # Fixture is a list of page dicts — wrap into ConfluenceSearchResult
    raw = load_fixture("confluence/search-proj.json")
    assert isinstance(raw, list)
    mcp_tokens = count_tokens(json.dumps(raw, ensure_ascii=False))

    search_data = {"results": raw, "total": len(raw), "start": 0, "limit": len(raw)}
    result = ConfluenceSearchResult.model_validate(search_data)
    compact_output = format_compact(result)
    compact_tokens = count_tokens(compact_output)

    print(f"\nScenario 4 - Confluence Search: mcp={mcp_tokens} compact={compact_tokens} ratio={compact_tokens/mcp_tokens:.2%}")
    assert compact_tokens < mcp_tokens * 0.5, (
        f"L1 fail: compact={compact_tokens} >= threshold={mcp_tokens * 0.5:.0f} (mcp={mcp_tokens})"
    )
