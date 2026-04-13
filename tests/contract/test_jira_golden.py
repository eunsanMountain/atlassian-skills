from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import TypeAdapter

from atlassian_skills.jira.models import (
    Board,
    Issue,
    Project,
    SearchResult,
    Sprint,
    Transition,
    WatcherList,
    WorklogList,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "jira"


def load(filename: str) -> object:
    return json.loads((FIXTURES / filename).read_text())


# ---------------------------------------------------------------------------
# get-issue-rlm3.json → Issue
# ---------------------------------------------------------------------------


class TestGoldenIssue:
    def test_golden_issue_parses(self) -> None:
        data = load("get-issue-rlm3.json")
        issue = Issue.model_validate(data)
        assert issue.key
        assert issue.summary
        assert issue.status is not None

    def test_golden_issue_key_format(self) -> None:
        data = load("get-issue-rlm3.json")
        issue = Issue.model_validate(data)
        assert re.match(r"^[A-Z]+-\d+$", issue.key), f"Key {issue.key!r} does not match PROJECT-NUMBER pattern"

    def test_golden_issue_fields_accessible(self) -> None:
        data = load("get-issue-rlm3.json")
        issue = Issue.model_validate(data)
        assert issue.assignee is not None
        assert issue.reporter is not None
        assert issue.priority is not None
        assert issue.issue_type is not None
        assert issue.created is not None
        assert issue.updated is not None

    def test_golden_issue_id_and_key(self) -> None:
        data = load("get-issue-rlm3.json")
        issue = Issue.model_validate(data)
        assert issue.id == "629816"
        assert issue.key == "RLM-3"

    def test_golden_issue_summary_is_str(self) -> None:
        data = load("get-issue-rlm3.json")
        issue = Issue.model_validate(data)
        assert isinstance(issue.summary, str)
        assert len(issue.summary) > 0

    def test_golden_issue_status_name(self) -> None:
        data = load("get-issue-rlm3.json")
        issue = Issue.model_validate(data)
        assert issue.status is not None
        assert issue.status.name == "To Do"

    def test_golden_issue_issue_type_name(self) -> None:
        data = load("get-issue-rlm3.json")
        issue = Issue.model_validate(data)
        assert issue.issue_type is not None
        assert issue.issue_type.name == "Epic"

    def test_golden_issue_priority_name(self) -> None:
        data = load("get-issue-rlm3.json")
        issue = Issue.model_validate(data)
        assert issue.priority is not None
        assert issue.priority.name == "Medium"

    def test_golden_issue_assignee_name(self) -> None:
        data = load("get-issue-rlm3.json")
        issue = Issue.model_validate(data)
        assert issue.assignee is not None
        assert issue.assignee.name == "seungmok.song"

    def test_golden_issue_reporter_name(self) -> None:
        data = load("get-issue-rlm3.json")
        issue = Issue.model_validate(data)
        assert issue.reporter is not None
        assert issue.reporter.name == "eunsan.jo"

    def test_golden_issue_description_is_str_or_none(self) -> None:
        data = load("get-issue-rlm3.json")
        issue = Issue.model_validate(data)
        assert issue.description is None or isinstance(issue.description, str)

    def test_golden_issue_labels_is_list(self) -> None:
        data = load("get-issue-rlm3.json")
        issue = Issue.model_validate(data)
        assert isinstance(issue.labels, list)

    def test_golden_issue_components_is_list(self) -> None:
        data = load("get-issue-rlm3.json")
        issue = Issue.model_validate(data)
        assert isinstance(issue.components, list)


# ---------------------------------------------------------------------------
# search-rlm.json → SearchResult
# ---------------------------------------------------------------------------


class TestGoldenSearchResult:
    def test_golden_search_result_parses(self) -> None:
        data = load("search-rlm.json")
        result = SearchResult.model_validate(data)
        assert result.total == 23
        assert isinstance(result.issues, list)

    def test_golden_search_total_matches_pagination(self) -> None:
        data = load("search-rlm.json")
        result = SearchResult.model_validate(data)
        assert result.total >= len(result.issues)

    def test_golden_search_issues_count(self) -> None:
        data = load("search-rlm.json")
        result = SearchResult.model_validate(data)
        assert len(result.issues) == 3

    def test_golden_search_start_at(self) -> None:
        data = load("search-rlm.json")
        result = SearchResult.model_validate(data)
        assert result.start_at == 0

    def test_golden_search_max_results(self) -> None:
        data = load("search-rlm.json")
        result = SearchResult.model_validate(data)
        assert result.max_results == 3

    def test_golden_search_each_issue_has_key(self) -> None:
        data = load("search-rlm.json")
        result = SearchResult.model_validate(data)
        for issue in result.issues:
            assert re.match(r"^[A-Z]+-\d+$", issue.key)


# ---------------------------------------------------------------------------
# get-transitions-rlm3.json → list[Transition]
# ---------------------------------------------------------------------------


class TestGoldenTransitions:
    def test_golden_transitions_parse(self) -> None:
        data = load("get-transitions-rlm3.json")
        adapter: TypeAdapter[list[Transition]] = TypeAdapter(list[Transition])
        transitions = adapter.validate_python(data)
        assert len(transitions) == 1

    def test_golden_transition_has_id_and_name(self) -> None:
        data = load("get-transitions-rlm3.json")
        adapter: TypeAdapter[list[Transition]] = TypeAdapter(list[Transition])
        transitions = adapter.validate_python(data)
        t = transitions[0]
        assert isinstance(t.id, int)
        assert isinstance(t.name, str)
        assert t.id == 11
        assert t.name == "To in planing"


# ---------------------------------------------------------------------------
# get-projects-sample.json → list[Project]
# ---------------------------------------------------------------------------


class TestGoldenProjects:
    def test_golden_projects_parse(self) -> None:
        data = load("get-projects-sample.json")
        adapter: TypeAdapter[list[Project]] = TypeAdapter(list[Project])
        projects = adapter.validate_python(data)
        assert len(projects) == 3

    def test_golden_projects_have_key_and_name(self) -> None:
        data = load("get-projects-sample.json")
        adapter: TypeAdapter[list[Project]] = TypeAdapter(list[Project])
        projects = adapter.validate_python(data)
        keys = [p.key for p in projects]
        assert "TEST" in keys
        assert "DEMO" in keys
        assert "SAMPLE" in keys

    def test_golden_project_lead_accessible(self) -> None:
        data = load("get-projects-sample.json")
        adapter: TypeAdapter[list[Project]] = TypeAdapter(list[Project])
        projects = adapter.validate_python(data)
        test_proj = next(p for p in projects if p.key == "TEST")
        assert test_proj.lead is not None
        assert test_proj.lead.name == "test.lead"


# ---------------------------------------------------------------------------
# get-comments-sample.json (Jira) — raw dict with 'comments' list
# ---------------------------------------------------------------------------


class TestGoldenJiraComments:
    def test_golden_comments_parse(self) -> None:
        data = load("get-comments-sample.json")
        assert isinstance(data, dict)
        assert "comments" in data
        comments = data["comments"]
        assert len(comments) == 3

    def test_golden_comments_fields(self) -> None:
        data = load("get-comments-sample.json")
        first = data["comments"][0]
        assert first["id"] == "10101"
        assert "author" in first
        assert "body" in first

    def test_golden_comments_pagination_fields(self) -> None:
        data = load("get-comments-sample.json")
        assert data["startAt"] == 0
        assert data["maxResults"] == 100
        assert data["total"] == 3


# ---------------------------------------------------------------------------
# get-watchers-rlm3.json → WatcherList
# ---------------------------------------------------------------------------


class TestGoldenWatchers:
    def test_golden_watchers_parse(self) -> None:
        data = load("get-watchers-rlm3.json")
        wl = WatcherList.model_validate(data)
        assert wl.issue_key == "RLM-3"
        assert wl.watcher_count == 1

    def test_golden_watchers_is_watching(self) -> None:
        data = load("get-watchers-rlm3.json")
        wl = WatcherList.model_validate(data)
        assert wl.is_watching is True

    def test_golden_watchers_user_fields(self) -> None:
        data = load("get-watchers-rlm3.json")
        wl = WatcherList.model_validate(data)
        assert len(wl.watchers) == 1
        watcher = wl.watchers[0]
        assert watcher.name == "eunsan.jo"
        assert watcher.email == "eunsan.jo@example.com"
        assert watcher.key == "JIRAUSER14505"


# ---------------------------------------------------------------------------
# get-worklog-rlm3.json → WorklogList
# ---------------------------------------------------------------------------


class TestGoldenWorklogs:
    def test_golden_worklogs_parse(self) -> None:
        data = load("get-worklog-rlm3.json")
        wl = WorklogList.model_validate(data)
        assert wl.worklogs == []

    def test_golden_worklog_list_type(self) -> None:
        data = load("get-worklog-rlm3.json")
        wl = WorklogList.model_validate(data)
        assert isinstance(wl.worklogs, list)


# ---------------------------------------------------------------------------
# get-agile-boards-rlm.json → list[Board]
# ---------------------------------------------------------------------------


class TestGoldenBoards:
    def test_golden_boards_parse(self) -> None:
        data = load("get-agile-boards-rlm.json")
        adapter: TypeAdapter[list[Board]] = TypeAdapter(list[Board])
        boards = adapter.validate_python(data)
        assert len(boards) == 2

    def test_golden_boards_types(self) -> None:
        data = load("get-agile-boards-rlm.json")
        adapter: TypeAdapter[list[Board]] = TypeAdapter(list[Board])
        boards = adapter.validate_python(data)
        types = {b.type for b in boards}
        assert "kanban" in types
        assert "scrum" in types

    def test_golden_board_ids(self) -> None:
        data = load("get-agile-boards-rlm.json")
        adapter: TypeAdapter[list[Board]] = TypeAdapter(list[Board])
        boards = adapter.validate_python(data)
        kanban = next(b for b in boards if b.type == "kanban")
        assert kanban.id == "395"
        assert kanban.name == "Kanban"


# ---------------------------------------------------------------------------
# get-sprints-sample.json → list[Sprint]
# ---------------------------------------------------------------------------


class TestGoldenSprints:
    def test_golden_sprints_parse(self) -> None:
        data = load("get-sprints-sample.json")
        adapter: TypeAdapter[list[Sprint]] = TypeAdapter(list[Sprint])
        sprints = adapter.validate_python(data["values"])
        assert len(sprints) == 2

    def test_golden_sprints_have_id_name_state(self) -> None:
        data = load("get-sprints-sample.json")
        adapter: TypeAdapter[list[Sprint]] = TypeAdapter(list[Sprint])
        sprints = adapter.validate_python(data["values"])
        sprint1 = next(s for s in sprints if s.id == 1)
        assert sprint1.name == "Sprint 1"
        assert sprint1.state == "active"

    def test_golden_sprint_future_state(self) -> None:
        data = load("get-sprints-sample.json")
        adapter: TypeAdapter[list[Sprint]] = TypeAdapter(list[Sprint])
        sprints = adapter.validate_python(data["values"])
        sprint2 = next(s for s in sprints if s.id == 2)
        assert sprint2.state == "future"
