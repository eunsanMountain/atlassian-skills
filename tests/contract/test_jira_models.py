from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from atlassian_skills.jira.models import (
    Board,
    Issue,
    JiraField,
    SearchResult,
    Status,
    Transition,
    User,
    WatcherList,
    WorklogList,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "jira"


def load(filename: str) -> object:
    return json.loads((FIXTURES / filename).read_text())


class TestIssueModel:
    def test_get_issue(self) -> None:
        data = load("get-issue-proj3.json")
        issue = Issue.model_validate(data)
        assert issue.id == "629816"
        assert issue.key == "PROJ-3"
        assert issue.summary == "Navi Map 통합-경로 판단 개선"
        assert issue.status.name == "To Do"
        assert issue.status.category == "To Do"
        assert issue.issue_type.name == "Epic"
        assert issue.priority is not None
        assert issue.priority.name == "Medium"

    def test_issue_assignee(self) -> None:
        data = load("get-issue-proj3.json")
        issue = Issue.model_validate(data)
        assert issue.assignee is not None
        assert issue.assignee.name == "testuser2"
        assert issue.assignee.email == "testuser2@example.com"

    def test_issue_reporter(self) -> None:
        data = load("get-issue-proj3.json")
        issue = Issue.model_validate(data)
        assert issue.reporter is not None
        assert issue.reporter.name == "testuser"
        assert issue.reporter.key == "JIRAUSER14505"

    def test_issue_timestamps(self) -> None:
        data = load("get-issue-proj3.json")
        issue = Issue.model_validate(data)
        assert issue.created is not None
        assert issue.updated is not None

    def test_issue_extra_fields_ignored(self) -> None:
        data = load("get-issue-proj3.json")
        assert isinstance(data, dict)
        data["unknown_field"] = "should be ignored"
        issue = Issue.model_validate(data)
        assert issue.key == "PROJ-3"

    def test_issue_customfield_extracted_for_json_output(self) -> None:
        data = load("get-issue-proj3.json")
        assert isinstance(data, dict)
        data.setdefault("fields", {})
        data["fields"]["customfield_10100"] = "PROJ-1"
        issue = Issue.model_validate(data)
        assert issue.custom_fields["customfield_10100"] == "PROJ-1"

    def test_issue_optional_fields_default(self) -> None:
        minimal: dict = {
            "id": "1",
            "key": "PROJ-1",
            "summary": "Test",
            "status": {"name": "Open"},
            "issue_type": {"name": "Bug"},
        }
        issue = Issue.model_validate(minimal)
        assert issue.description is None
        assert issue.labels == []
        assert issue.components == []


class TestSearchResultModel:
    def test_search_result(self) -> None:
        data = load("search-proj.json")
        result = SearchResult.model_validate(data)
        assert result.total == 23
        assert result.start_at == 0
        assert result.max_results == 3
        assert len(result.issues) == 3

    def test_search_result_issue_keys(self) -> None:
        data = load("search-proj.json")
        result = SearchResult.model_validate(data)
        keys = [i.key for i in result.issues]
        assert "PROJ-3" in keys
        assert "PROJ-24" in keys
        assert "PROJ-23" in keys

    def test_search_result_issue_types(self) -> None:
        data = load("search-proj.json")
        result = SearchResult.model_validate(data)
        epic = next(i for i in result.issues if i.key == "PROJ-3")
        assert epic.issue_type.name == "Epic"
        story = next(i for i in result.issues if i.key == "PROJ-24")
        assert story.issue_type.name == "Story"


class TestTransitionModel:
    def test_transition_list(self) -> None:
        data = load("get-transitions-rlm3.json")
        adapter: TypeAdapter[list[Transition]] = TypeAdapter(list[Transition])
        transitions = adapter.validate_python(data)
        assert len(transitions) == 1
        assert transitions[0].id == 11
        assert transitions[0].name == "To in planing"

    def test_transition_extra_ignored(self) -> None:
        raw = [{"id": 1, "name": "In Progress", "extra": "ignored"}]
        adapter: TypeAdapter[list[Transition]] = TypeAdapter(list[Transition])
        transitions = adapter.validate_python(raw)
        assert transitions[0].name == "In Progress"


class TestJiraFieldModel:
    def test_field_list(self) -> None:
        data = load("search-fields-epic.json")
        adapter: TypeAdapter[list[JiraField]] = TypeAdapter(list[JiraField])
        fields = adapter.validate_python(data)
        assert len(fields) == 4

    def test_field_attributes(self) -> None:
        data = load("search-fields-epic.json")
        adapter: TypeAdapter[list[JiraField]] = TypeAdapter(list[JiraField])
        fields = adapter.validate_python(data)
        epic_link = fields[0]
        assert epic_link.id == "customfield_10100"
        assert epic_link.name == "Epic Link"
        assert epic_link.custom is True

    def test_field_clause_names_alias(self) -> None:
        data = load("search-fields-epic.json")
        adapter: TypeAdapter[list[JiraField]] = TypeAdapter(list[JiraField])
        fields = adapter.validate_python(data)
        assert "Epic Link" in fields[0].clause_names

    def test_field_schema_alias(self) -> None:
        data = load("search-fields-epic.json")
        adapter: TypeAdapter[list[JiraField]] = TypeAdapter(list[JiraField])
        fields = adapter.validate_python(data)
        assert fields[0].field_schema is not None
        assert fields[0].field_schema.type == "any"


class TestBoardModel:
    def test_board_list(self) -> None:
        data = load("get-agile-boards-rlm.json")
        adapter: TypeAdapter[list[Board]] = TypeAdapter(list[Board])
        boards = adapter.validate_python(data)
        assert len(boards) == 2

    def test_board_attributes(self) -> None:
        data = load("get-agile-boards-rlm.json")
        adapter: TypeAdapter[list[Board]] = TypeAdapter(list[Board])
        boards = adapter.validate_python(data)
        kanban = next(b for b in boards if b.type == "kanban")
        assert kanban.id == 395
        assert kanban.name == "Kanban"
        scrum = next(b for b in boards if b.type == "scrum")
        assert scrum.id == 394


class TestWorklogListModel:
    def test_empty_worklog(self) -> None:
        data = load("get-worklog-rlm3.json")
        wl = WorklogList.model_validate(data)
        assert wl.worklogs == []

    def test_worklog_with_entries(self) -> None:
        data = {
            "worklogs": [
                {
                    "id": "10001",
                    "author": {"display_name": "Alice", "name": "alice"},
                    "time_spent": "1h",
                    "time_spent_seconds": 3600,
                    "started": "2026-04-01T09:00:00.000+0900",
                }
            ]
        }
        wl = WorklogList.model_validate(data)
        assert len(wl.worklogs) == 1
        assert wl.worklogs[0].time_spent == "1h"
        assert wl.worklogs[0].author is not None
        assert wl.worklogs[0].author.name == "alice"


class TestWatcherListModel:
    def test_watcher_list(self) -> None:
        data = load("get-watchers-proj3.json")
        wl = WatcherList.model_validate(data)
        assert wl.issue_key == "PROJ-3"
        assert wl.watcher_count == 1
        assert wl.is_watching is True
        assert len(wl.watchers) == 1

    def test_watcher_user_fields(self) -> None:
        data = load("get-watchers-proj3.json")
        wl = WatcherList.model_validate(data)
        watcher = wl.watchers[0]
        assert watcher.name == "testuser"
        assert watcher.key == "JIRAUSER14505"
        assert watcher.email == "testuser@example.com"


# ---------------------------------------------------------------------------
# New tests (+15)
# ---------------------------------------------------------------------------


class TestIssueMissingOptionalFields:
    def test_no_assignee(self) -> None:
        data: dict = {
            "id": "1",
            "key": "PROJ-1",
            "summary": "No assignee",
            "status": {"name": "Open"},
            "issue_type": {"name": "Bug"},
        }
        issue = Issue.model_validate(data)
        assert issue.assignee is None

    def test_no_priority(self) -> None:
        data: dict = {
            "id": "1",
            "key": "PROJ-1",
            "summary": "No priority",
            "status": {"name": "Open"},
            "issue_type": {"name": "Task"},
        }
        issue = Issue.model_validate(data)
        assert issue.priority is None

    def test_no_description(self) -> None:
        data: dict = {
            "id": "1",
            "key": "PROJ-1",
            "summary": "No description",
            "status": {"name": "Open"},
            "issue_type": {"name": "Task"},
        }
        issue = Issue.model_validate(data)
        assert issue.description is None

    def test_extra_unknown_fields_ignored(self) -> None:
        """ConfigDict extra=ignore: unknown fields do not raise."""
        data: dict = {
            "id": "1",
            "key": "PROJ-2",
            "summary": "Extra fields",
            "status": {"name": "Open"},
            "issue_type": {"name": "Bug"},
            "totally_unknown_field": "should be silently ignored",
            "another_random_key": 42,
        }
        issue = Issue.model_validate(data)
        assert issue.key == "PROJ-2"
        assert not hasattr(issue, "totally_unknown_field")


class TestStatusMinimal:
    def test_status_name_only_no_category(self) -> None:
        s = Status.model_validate({"name": "Done"})
        assert s.name == "Done"
        assert s.category is None
        assert s.color is None


class TestUserMinimal:
    def test_user_name_only_no_email(self) -> None:
        u = User.model_validate({"displayName": "Alice", "name": "alice"})
        assert u.display_name == "Alice"
        assert u.name == "alice"
        assert u.email is None


class TestSearchResultEdgeCases:
    def test_zero_issues(self) -> None:
        data: dict = {"total": 0, "start_at": 0, "max_results": 50, "issues": []}
        result = SearchResult.model_validate(data)
        assert result.total == 0
        assert result.issues == []

    def test_pagination_start_at(self) -> None:
        """start_at > 0 represents a paginated response."""
        data: dict = {
            "total": 100,
            "start_at": 50,
            "max_results": 50,
            "issues": [],
        }
        result = SearchResult.model_validate(data)
        assert result.start_at == 50
        assert result.total == 100


class TestTransitionIntegerId:
    def test_integer_id_coercion(self) -> None:
        """Transition.id is int; providing an int directly must work."""
        raw = [{"id": 21, "name": "Done"}]
        adapter: TypeAdapter[list[Transition]] = TypeAdapter(list[Transition])
        transitions = adapter.validate_python(raw)
        assert transitions[0].id == 21
        assert isinstance(transitions[0].id, int)


class TestBoardMissingType:
    def test_board_missing_type_raises(self) -> None:
        """Board.type has no default — missing it must raise a validation error."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Board.model_validate({"id": "1", "name": "My Board"})


class TestJiraFieldEdgeCases:
    def test_no_schema(self) -> None:
        data: dict = {"id": "summary", "name": "Summary", "clauseNames": ["summary"]}
        field = JiraField.model_validate(data)
        assert field.field_schema is None

    def test_clause_names_access(self) -> None:
        data: dict = {
            "id": "cf_10100",
            "name": "Epic Link",
            "clauseNames": ["Epic Link", "cf[10100]"],
        }
        field = JiraField.model_validate(data)
        assert "Epic Link" in field.clause_names
        assert "cf[10100]" in field.clause_names


class TestWorklogListMultiple:
    def test_multiple_worklogs(self) -> None:
        data: dict = {
            "worklogs": [
                {"id": "1", "author": {"displayName": "Alice"}, "time_spent": "1h"},
                {"id": "2", "author": {"displayName": "Bob"}, "time_spent": "30m"},
            ],
            "total": 2,
        }
        wl = WorklogList.model_validate(data)
        assert len(wl.worklogs) == 2
        assert wl.total == 2


class TestWatcherListZero:
    def test_zero_watchers(self) -> None:
        data: dict = {
            "issue_key": "PROJ-1",
            "watchCount": 0,
            "isWatching": False,
            "watchers": [],
        }
        wl = WatcherList.model_validate(data)
        assert wl.watcher_count == 0
        assert wl.watchers == []


class TestIssueRawApiFormat:
    """Issue.model_validate works with raw Jira REST API v2 response (fields envelope)."""

    def test_issue_from_raw_api_response(self) -> None:
        raw = {
            "id": "123",
            "key": "TEST-1",
            "fields": {
                "summary": "Test issue",
                "status": {"name": "Open"},
                "issuetype": {"name": "Bug"},
                "priority": {"name": "High"},
                "assignee": {"displayName": "Test User", "name": "testuser"},
                "reporter": {"displayName": "Reporter", "name": "reporter"},
                "created": "2026-01-01T00:00:00.000+0000",
                "updated": "2026-01-02T00:00:00.000+0000",
            },
        }
        issue = Issue.model_validate(raw)
        assert issue.key == "TEST-1"
        assert issue.summary == "Test issue"
        assert issue.status is not None
        assert issue.status.name == "Open"
        assert issue.issue_type is not None
        assert issue.issue_type.name == "Bug"
        assert issue.assignee is not None
        assert issue.assignee.display_name == "Test User"
        assert issue.created == "2026-01-01T00:00:00.000+0000"

    def test_issue_from_raw_api_preserves_top_level_fields(self) -> None:
        """Top-level id/key must survive flattening."""
        raw = {
            "id": "999",
            "key": "RAW-5",
            "fields": {
                "summary": "Raw test",
                "status": {"name": "Done"},
                "issuetype": {"name": "Task"},
            },
        }
        issue = Issue.model_validate(raw)
        assert issue.id == "999"
        assert issue.key == "RAW-5"

    def test_issue_flattened_format_still_works(self) -> None:
        """Pre-flattened fixtures (no 'fields' key) continue to work."""
        flat = {
            "id": "1",
            "key": "PROJ-1",
            "summary": "Already flat",
            "status": {"name": "Open"},
            "issue_type": {"name": "Bug"},
        }
        issue = Issue.model_validate(flat)
        assert issue.summary == "Already flat"


class TestSearchResultRawApiFormat:
    """SearchResult.model_validate works with raw Jira REST API v2 search response."""

    def test_search_result_from_raw_api(self) -> None:
        raw = {
            "total": 1,
            "startAt": 0,
            "maxResults": 50,
            "issues": [
                {
                    "id": "1",
                    "key": "T-1",
                    "fields": {
                        "summary": "X",
                        "status": {"name": "Open"},
                    },
                }
            ],
        }
        result = SearchResult.model_validate(raw)
        assert result.total == 1
        assert result.start_at == 0
        assert result.max_results == 50
        assert len(result.issues) == 1
        assert result.issues[0].summary == "X"
        assert result.issues[0].key == "T-1"

    def test_search_result_snake_case_still_works(self) -> None:
        """Pre-processed snake_case keys still work via populate_by_name."""
        data = {
            "total": 0,
            "start_at": 0,
            "max_results": 10,
            "issues": [],
        }
        result = SearchResult.model_validate(data)
        assert result.total == 0
        assert result.max_results == 10


class TestModelDumpMethods:
    """All core models: model_dump() → dict, model_dump_json() → valid JSON string."""

    def _minimal_issue(self) -> Issue:
        return Issue.model_validate(
            {
                "id": "1",
                "key": "PROJ-1",
                "summary": "Test",
                "status": {"name": "Open"},
                "issue_type": {"name": "Bug"},
            }
        )

    def test_issue_model_dump(self) -> None:
        d = self._minimal_issue().model_dump()
        assert isinstance(d, dict)
        assert d["key"] == "PROJ-1"

    def test_issue_model_dump_json(self) -> None:
        js = self._minimal_issue().model_dump_json()
        parsed = json.loads(js)
        assert parsed["key"] == "PROJ-1"

    def test_search_result_model_dump(self) -> None:
        result = SearchResult.model_validate({"total": 0, "start_at": 0, "max_results": 10, "issues": []})
        d = result.model_dump()
        assert isinstance(d, dict)
        assert d["total"] == 0

    def test_search_result_model_dump_json(self) -> None:
        result = SearchResult.model_validate({"total": 0, "start_at": 0, "max_results": 10, "issues": []})
        js = result.model_dump_json()
        assert json.loads(js)["total"] == 0

    def test_transition_model_dump(self) -> None:
        t = Transition.model_validate({"id": 1, "name": "Start"})
        d = t.model_dump()
        assert isinstance(d, dict)
        assert d["name"] == "Start"

    def test_transition_model_dump_json(self) -> None:
        t = Transition.model_validate({"id": 1, "name": "Start"})
        js = t.model_dump_json()
        assert json.loads(js)["id"] == 1

    def test_watcher_list_model_dump(self) -> None:
        wl = WatcherList.model_validate({"watchCount": 0, "isWatching": False, "watchers": []})
        d = wl.model_dump()
        assert isinstance(d, dict)

    def test_worklog_list_model_dump(self) -> None:
        wll = WorklogList.model_validate({"worklogs": []})
        d = wll.model_dump()
        assert isinstance(d, dict)
        assert d["worklogs"] == []
