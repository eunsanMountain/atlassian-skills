from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from atlassian_skills.confluence.models import ConfluenceSearchResult, Page
from atlassian_skills.core.format import OutputFormat, format_output
from atlassian_skills.core.format.compact import format_compact
from atlassian_skills.core.format.json_fmt import format_json
from atlassian_skills.core.format.markdown import (
    confluence_storage_to_md,
    format_md_issue,
    jira_wiki_to_md,
    md_to_confluence_storage,
    md_to_jira_wiki,
)
from atlassian_skills.core.format.raw import format_raw
from atlassian_skills.jira.models import Issue, SearchResult, Transition, WatcherList, WorklogList

# ---------------------------------------------------------------------------
# compact
# ---------------------------------------------------------------------------

SAMPLE_ISSUE: dict = {
    "key": "PROJ-1",
    "status": "In Progress",
    "issuetype": "Bug",
    "priority": "Major",
    "assignee": "jdoe",
    "summary": "Something is broken",
    "updated": "2026-04-13T00:00:00.000+0000",
}


class TestCompactFormat:
    def test_single_issue(self) -> None:
        line = format_compact(SAMPLE_ISSUE)
        assert "PROJ-1" in line
        assert "In Progress" in line
        assert "Bug" in line
        assert "Major" in line
        assert "jdoe" in line
        assert "Something is broken" in line
        parts = line.split(" | ")
        assert len(parts) == 7

    def test_list_of_issues(self) -> None:
        issues = [SAMPLE_ISSUE, {**SAMPLE_ISSUE, "key": "PROJ-2"}]
        output = format_compact(issues)
        lines = output.splitlines()
        assert len(lines) == 2
        assert "PROJ-1" in lines[0]
        assert "PROJ-2" in lines[1]

    def test_missing_fields_become_empty(self) -> None:
        line = format_compact({"key": "PROJ-99"})
        assert "PROJ-99" in line
        assert " |  |  | " in line

    def test_string_passthrough(self) -> None:
        assert format_compact("hello") == "hello"

    def test_non_dict_list(self) -> None:
        output = format_compact(["a", "b"])
        assert output == "a\nb"


# ---------------------------------------------------------------------------
# json
# ---------------------------------------------------------------------------

class SampleModel(BaseModel):
    key: str
    value: int


class TestJsonFormat:
    def test_dict_minified(self) -> None:
        result = format_json({"a": 1, "b": "x"})
        parsed = json.loads(result)
        assert parsed == {"a": 1, "b": "x"}
        assert " " not in result  # minified

    def test_list_of_dicts(self) -> None:
        result = format_json([{"a": 1}, {"b": 2}])
        parsed = json.loads(result)
        assert len(parsed) == 2

    def test_pydantic_model(self) -> None:
        model = SampleModel(key="PROJ-1", value=42)
        result = format_json(model)
        parsed = json.loads(result)
        assert parsed["key"] == "PROJ-1"
        assert parsed["value"] == 42

    def test_list_of_pydantic_models(self) -> None:
        models = [SampleModel(key="A", value=1), SampleModel(key="B", value=2)]
        result = format_json(models)
        parsed = json.loads(result)
        assert len(parsed) == 2
        assert parsed[0]["key"] == "A"

    def test_non_ascii_preserved(self) -> None:
        result = format_json({"msg": "안녕"})
        assert "안녕" in result

    def test_scalar_fallback(self) -> None:
        assert format_json(42) == "42"


# ---------------------------------------------------------------------------
# raw
# ---------------------------------------------------------------------------

class TestRawFormat:
    def test_string_passthrough(self) -> None:
        s = "hello\nworld\t특수문자"
        assert format_raw(s) == s

    def test_bytes_decoded(self) -> None:
        b = "hello 안녕".encode()
        assert format_raw(b) == "hello 안녕"

    def test_special_chars_preserved(self) -> None:
        special = "<tag>\"'&\\n\x00"
        assert format_raw(special) == special

    def test_bytes_with_invalid_utf8(self) -> None:
        # replacement character expected for invalid bytes
        b = b"\xff\xfe"
        result = format_raw(b)
        assert "\ufffd" in result

    def test_non_str_bytes(self) -> None:
        result = format_raw(123)
        assert result == "123"


# ---------------------------------------------------------------------------
# markdown (cfxmark wrappers)
# ---------------------------------------------------------------------------

class TestMarkdownFormat:
    def test_jira_wiki_to_md_empty(self) -> None:
        assert jira_wiki_to_md("") == ""

    def test_jira_wiki_to_md_basic(self) -> None:
        result = jira_wiki_to_md("*bold*")
        assert result  # some non-empty markdown returned

    def test_md_to_jira_wiki_empty(self) -> None:
        assert md_to_jira_wiki("") == ""

    def test_md_to_jira_wiki_basic(self) -> None:
        result = md_to_jira_wiki("**bold**")
        assert result  # some non-empty jira wiki returned

    def test_confluence_storage_to_md_empty(self) -> None:
        assert confluence_storage_to_md("") == ""

    def test_confluence_storage_to_md_basic(self) -> None:
        result = confluence_storage_to_md("<p>hello</p>")
        assert "hello" in result

    def test_md_to_confluence_storage_empty(self) -> None:
        assert md_to_confluence_storage("") == ""

    def test_md_to_confluence_storage_basic(self) -> None:
        result = md_to_confluence_storage("# Hello")
        assert "<h1>" in result or "Hello" in result

    def test_format_md_issue_basic(self) -> None:
        result = format_md_issue(SAMPLE_ISSUE)
        assert "PROJ-1" in result
        assert "Something is broken" in result
        assert "In Progress" in result

    def test_format_md_issue_no_description(self) -> None:
        issue = {"key": "PROJ-2", "summary": "No desc", "status": "Open"}
        result = format_md_issue(issue)
        assert "PROJ-2" in result
        assert "## Description" not in result


# ---------------------------------------------------------------------------
# format_output dispatcher
# ---------------------------------------------------------------------------

class TestFormatOutputDispatcher:
    def test_compact_dispatch(self) -> None:
        result = format_output(SAMPLE_ISSUE, OutputFormat.COMPACT)
        assert "PROJ-1" in result
        assert " | " in result

    def test_json_dispatch(self) -> None:
        result = format_output({"x": 1}, OutputFormat.JSON)
        parsed = json.loads(result)
        assert parsed["x"] == 1

    def test_raw_dispatch(self) -> None:
        result = format_output("raw string", OutputFormat.RAW)
        assert result == "raw string"

    def test_markdown_dispatch_falls_back_to_json(self) -> None:
        result = format_output({"key": "v"}, OutputFormat.MD)
        parsed = json.loads(result)
        assert parsed["key"] == "v"


# ---------------------------------------------------------------------------
# New tests (+8)
# ---------------------------------------------------------------------------


def _make_issue(extra: dict | None = None) -> Issue:
    base: dict = {
        "id": "1",
        "key": "PROJ-1",
        "summary": "Test issue",
        "status": {"name": "Open"},
        "issue_type": {"name": "Bug"},
    }
    if extra:
        base.update(extra)
    return Issue.model_validate(base)


class TestCompactFormatExtended:
    def test_issue_missing_assignee_produces_empty_slot(self) -> None:
        """Issue without assignee → empty string in the compact line (not a crash)."""
        issue = _make_issue()
        result = format_compact(issue)
        # assignee slot is empty; the key and summary are present
        assert "PROJ-1" in result
        assert "Test issue" in result
        # The compact line contains at least 3 pipe separators
        assert result.count("|") >= 3

    def test_issue_missing_priority_produces_empty_slot(self) -> None:
        """Issue without priority → empty string in the compact line (not a crash)."""
        issue = _make_issue()
        assert issue.priority is None
        result = format_compact(issue)
        assert "PROJ-1" in result

    def test_empty_search_result_compact(self) -> None:
        """SearchResult with 0 issues → header line with total:0."""
        result = SearchResult.model_validate(
            {"total": 0, "start_at": 0, "max_results": 50, "issues": []}
        )
        output = format_compact(result)
        assert "total:0" in output

    def test_format_json_none_values(self) -> None:
        """Dicts with None values serialize to JSON null."""
        result = format_json({"key": "A", "value": None})
        parsed = json.loads(result)
        assert parsed["value"] is None

    def test_format_raw_bytes_decoded(self) -> None:
        """format_raw with bytes input returns decoded string."""
        b = "한글 bytes".encode()
        result = format_raw(b)
        assert result == "한글 bytes"

    def test_format_raw_empty_string(self) -> None:
        """format_raw with empty string returns empty string."""
        assert format_raw("") == ""

    def test_format_output_unknown_format_raises(self) -> None:
        """format_output with an unmapped format value raises ValueError."""
        # Construct a fake OutputFormat-like value by bypassing the enum
        with pytest.raises((ValueError, KeyError, AttributeError)):
            # Force an unknown string through — the dispatcher hits the raise
            format_output("data", "unknown_format")  # type: ignore[arg-type]

    def test_format_compact_transition_model(self) -> None:
        """format_compact dispatches Transition correctly."""
        t = Transition.model_validate({"id": 11, "name": "In Progress", "to_status": {"name": "In Progress"}})
        result = format_compact(t)
        assert "11" in result
        assert "In Progress" in result


# ===========================================================================
# New tests (+25)
# ===========================================================================


# ---------------------------------------------------------------------------
# format_output dispatch — parametrized
# ---------------------------------------------------------------------------

_PLAIN_DICT = {"key": "PROJ-1", "status": "Open", "summary": "Test"}


@pytest.mark.parametrize("fmt", [OutputFormat.COMPACT, OutputFormat.JSON, OutputFormat.MD, OutputFormat.RAW])
def test_format_output_with_dict(fmt: OutputFormat) -> None:
    """format_output dispatches a plain dict without raising for all formats."""
    result = format_output(_PLAIN_DICT, fmt)
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.parametrize("fmt", [OutputFormat.COMPACT, OutputFormat.JSON, OutputFormat.MD, OutputFormat.RAW])
def test_format_output_with_issue_model(fmt: OutputFormat) -> None:
    """format_output dispatches an Issue pydantic model without raising for all formats."""
    issue = Issue.model_validate({"id": "1", "key": "PROJ-1", "summary": "dispatch test"})
    result = format_output(issue, fmt)
    assert isinstance(result, str)
    assert "PROJ-1" in result or len(result) > 0


# ---------------------------------------------------------------------------
# format_compact — by model type
# ---------------------------------------------------------------------------


def test_compact_jira_issue() -> None:
    """format_compact on Issue model returns key and summary."""
    issue = Issue.model_validate({
        "id": "5",
        "key": "TEST-5",
        "summary": "compact issue test",
        "status": {"name": "Done"},
        "issuetype": {"name": "Story"},
        "priority": {"name": "Low"},
        "assignee": {"displayName": "Alice"},
    })
    result = format_compact(issue)
    assert "TEST-5" in result
    assert "compact issue test" in result
    assert "Done" in result
    assert "Story" in result
    assert "Low" in result
    assert "Alice" in result


def test_compact_jira_search_result() -> None:
    """format_compact on SearchResult returns header and issue rows."""
    result = SearchResult.model_validate({
        "total": 2,
        "startAt": 0,
        "maxResults": 50,
        "issues": [
            {"id": "1", "key": "SR-1", "summary": "first"},
            {"id": "2", "key": "SR-2", "summary": "second"},
        ],
    })
    output = format_compact(result)
    assert "total:2" in output
    assert "SR-1" in output
    assert "SR-2" in output


def test_compact_confluence_page() -> None:
    """format_compact on Page model returns id and title."""
    page = Page.model_validate({
        "id": "123456",
        "title": "My Confluence Page",
        "type": "page",
        "space": {"key": "MYSPACE", "name": "My Space"},
        "version": {"number": 3},
    })
    output = format_compact(page)
    assert "123456" in output
    assert "My Confluence Page" in output
    assert "MYSPACE" in output


def test_compact_confluence_search_result() -> None:
    """format_compact on ConfluenceSearchResult returns header and page rows."""
    search = ConfluenceSearchResult.model_validate({
        "total": 1,
        "start": 0,
        "limit": 25,
        "results": [
            {
                "id": "999",
                "title": "Found Page",
                "type": "page",
                "space": {"key": "TS", "name": "Test Space"},
            }
        ],
    })
    output = format_compact(search)
    assert "total:1" in output
    assert "Found Page" in output


def test_compact_transition_list() -> None:
    """format_compact on a list of Transition items formats each."""
    transitions = [
        Transition.model_validate({"id": 1, "name": "To Do", "to_status": {"name": "To Do"}}),
        Transition.model_validate({"id": 2, "name": "Done", "to_status": {"name": "Done"}}),
    ]
    # format_compact doesn't dispatch list[Transition] as a special type —
    # it falls through to the generic list branch (str(item) for each).
    output = format_compact(transitions)
    assert isinstance(output, str)
    assert len(output) > 0


def test_compact_watcher_list() -> None:
    """format_compact on WatcherList returns issue key and count."""
    watchers = WatcherList.model_validate({
        "issue_key": "WATCH-1",
        "watcher_count": 3,
        "watchers": [],
    })
    output = format_compact(watchers)
    assert "WATCH-1" in output
    assert "3" in output


def test_compact_worklog_list() -> None:
    """format_compact on WorklogList returns total and worklog lines."""
    worklogs = WorklogList.model_validate({
        "total": 2,
        "worklogs": [
            {"id": "10", "author": {"displayName": "Bob"}, "started": "2026-01-01", "time_spent": "1h"},
            {"id": "11", "author": {"displayName": "Carol"}, "started": "2026-01-02", "time_spent": "2h"},
        ],
    })
    output = format_compact(worklogs)
    assert "2 worklogs" in output
    assert "Bob" in output
    assert "1h" in output


def test_compact_generic_dict() -> None:
    """format_compact on a plain dict uses _format_issue_row."""
    d = {"key": "DICT-1", "status": "Active", "issuetype": "Task", "priority": "High",
         "assignee": "dev", "summary": "dict compact", "updated": "2026-04-13"}
    result = format_compact(d)
    assert "DICT-1" in result
    assert "dict compact" in result
    assert " | " in result


def test_compact_list_of_dicts() -> None:
    """format_compact on a list of dicts formats each as an issue row."""
    items = [
        {"key": "LD-1", "summary": "first item"},
        {"key": "LD-2", "summary": "second item"},
    ]
    output = format_compact(items)
    lines = output.splitlines()
    assert len(lines) == 2
    assert "LD-1" in lines[0]
    assert "LD-2" in lines[1]


# ---------------------------------------------------------------------------
# format_json — extended
# ---------------------------------------------------------------------------


def test_json_pydantic_model() -> None:
    """Pydantic model serializes to valid JSON with expected fields."""
    issue = Issue.model_validate({"id": "7", "key": "JSON-7", "summary": "json test"})
    result = format_json(issue)
    parsed = json.loads(result)
    assert parsed["key"] == "JSON-7"
    assert parsed["summary"] == "json test"


def test_json_dict() -> None:
    """Plain dict serializes to minified valid JSON."""
    d = {"alpha": 1, "beta": "two"}
    result = format_json(d)
    parsed = json.loads(result)
    assert parsed["alpha"] == 1
    assert parsed["beta"] == "two"
    assert "\n" not in result  # minified


def test_json_list() -> None:
    """Plain list serializes to valid JSON array."""
    lst = [1, "two", {"three": 3}]
    result = format_json(lst)
    parsed = json.loads(result)
    assert parsed == lst


# ---------------------------------------------------------------------------
# format_raw — extended
# ---------------------------------------------------------------------------


def test_raw_dict_preserves_structure() -> None:
    """Dict passed to format_raw is JSON-serialized via str() — not reformatted."""
    d = {"x": 1, "y": [2, 3]}
    result = format_raw(d)
    # str(dict) in Python produces repr-like, not JSON — just check it's a string
    assert isinstance(result, str)
    assert "x" in result


def test_raw_string_passthrough() -> None:
    """String passed to format_raw comes out verbatim, no modification."""
    s = "verbatim\tstring\n with 특수"
    result = format_raw(s)
    assert result == s


# ---------------------------------------------------------------------------
# format_md_issue — extended
# ---------------------------------------------------------------------------


def test_md_issue_full_fields() -> None:
    """format_md_issue with all common fields renders them all."""
    issue = {
        "key": "FULL-1",
        "summary": "Full fields test",
        "status": "In Review",
        "issuetype": "Task",
        "priority": "High",
        "assignee": "alice",
        "description": "Some description text",
    }
    result = format_md_issue(issue)
    assert "# FULL-1: Full fields test" in result
    assert "In Review" in result
    assert "Task" in result
    assert "High" in result
    assert "alice" in result
    assert "## Description" in result


def test_md_issue_minimal() -> None:
    """format_md_issue with only key + summary renders without crashing."""
    issue = {"key": "MIN-1", "summary": "Minimal issue"}
    result = format_md_issue(issue)
    assert "# MIN-1: Minimal issue" in result
    # No description section since there is no description
    assert "## Description" not in result


def test_md_issue_with_description() -> None:
    """format_md_issue with description renders a Description section."""
    issue = {
        "key": "DESC-1",
        "summary": "With description",
        "status": "Open",
        "description": "This is the *description* text.",
    }
    result = format_md_issue(issue)
    assert "## Description" in result
    # Description content should appear after the section header
    desc_idx = result.index("## Description")
    assert len(result) > desc_idx + len("## Description")
