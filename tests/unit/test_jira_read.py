from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from atlassian_skills.core.auth import Credential
from atlassian_skills.jira.client import JiraClient
from atlassian_skills.jira.models import (
    Board,
    Issue,
    JiraField,
    SearchResult,
    Transition,
    WatcherList,
    WorklogList,
)
from atlassian_skills.jira.preprocessing import (
    normalize_smart_links,
    preprocess_jira_text,
    replace_mentions,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "jira"
BASE_URL = "https://jira.example.com"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load(name: str) -> dict | list:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def cred() -> Credential:
    return Credential(method="pat", token="test-token")


@pytest.fixture
def client(cred: Credential) -> JiraClient:
    return JiraClient(BASE_URL, cred)


# ---------------------------------------------------------------------------
# get_issue
# ---------------------------------------------------------------------------


@respx.mock
def test_get_issue_returns_issue(client: JiraClient) -> None:
    fixture = _load("get-issue-rlm3.json")
    respx.get(f"{BASE_URL}/rest/api/2/issue/RLM-3").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    issue = client.get_issue("RLM-3")

    assert isinstance(issue, Issue)
    assert issue.key == "RLM-3"
    assert issue.id == "629816"
    assert issue.summary == "Navi Map 통합-경로 판단 개선"
    assert issue.status.name == "To Do"
    assert issue.issue_type.name == "Epic"
    assert issue.assignee is not None
    assert issue.assignee.name == "seungmok.song"


@respx.mock
def test_get_issue_raw_preserves_requested_customfield(client: JiraClient) -> None:
    fixture = _load("get-issue-rlm3.json")
    assert isinstance(fixture, dict)
    fixture.setdefault("fields", {})
    fixture["fields"]["customfield_10100"] = "EPIC-1"
    respx.get(f"{BASE_URL}/rest/api/2/issue/RLM-3").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    issue = client.get_issue_raw("RLM-3", fields=["customfield_10100"])

    assert issue["fields"]["customfield_10100"] == "EPIC-1"


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@respx.mock
def test_search_returns_search_result(client: JiraClient) -> None:
    fixture = _load("search-rlm.json")
    respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    result = client.search("project=RLM")

    assert isinstance(result, SearchResult)
    assert result.total == 23
    assert result.max_results == 3
    assert len(result.issues) == 3
    assert result.issues[0].key == "RLM-3"
    assert result.issues[1].key == "RLM-24"


# ---------------------------------------------------------------------------
# get_transitions
# ---------------------------------------------------------------------------


@respx.mock
def test_get_transitions_returns_list(client: JiraClient) -> None:
    fixture = _load("get-transitions-rlm3.json")
    respx.get(f"{BASE_URL}/rest/api/2/issue/RLM-3/transitions").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    transitions = client.get_transitions("RLM-3")

    assert isinstance(transitions, list)
    assert len(transitions) == 1
    assert isinstance(transitions[0], Transition)
    assert transitions[0].id == 11
    assert transitions[0].name == "To in planing"


@respx.mock
def test_get_transitions_wrapped_dict(client: JiraClient) -> None:
    """Real Jira API wraps transitions in a dict."""
    respx.get(f"{BASE_URL}/rest/api/2/issue/RLM-1/transitions").mock(
        return_value=httpx.Response(200, json={"transitions": [{"id": 21, "name": "In Progress"}]})
    )

    transitions = client.get_transitions("RLM-1")

    assert len(transitions) == 1
    assert transitions[0].name == "In Progress"


# ---------------------------------------------------------------------------
# search_fields — fuzzy match
# ---------------------------------------------------------------------------


@respx.mock
def test_search_fields_no_keyword(client: JiraClient) -> None:
    fixture = _load("search-fields-epic.json")
    respx.get(f"{BASE_URL}/rest/api/2/field").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    fields = client.search_fields()

    assert len(fields) == 4
    assert all(isinstance(f, JiraField) for f in fields)


@respx.mock
def test_search_fields_fuzzy_match(client: JiraClient) -> None:
    fixture = _load("search-fields-epic.json")
    respx.get(f"{BASE_URL}/rest/api/2/field").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    fields = client.search_fields(keyword="epic link")

    assert len(fields) == 1
    assert fields[0].name == "Epic Link"


@respx.mock
def test_search_fields_no_match(client: JiraClient) -> None:
    fixture = _load("search-fields-epic.json")
    respx.get(f"{BASE_URL}/rest/api/2/field").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    fields = client.search_fields(keyword="nonexistent")

    assert fields == []


# ---------------------------------------------------------------------------
# list_boards
# ---------------------------------------------------------------------------


@respx.mock
def test_list_boards_returns_boards(client: JiraClient) -> None:
    fixture = _load("get-agile-boards-rlm.json")
    respx.get(f"{BASE_URL}/rest/agile/1.0/board").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    boards = client.list_boards()

    assert len(boards) == 2
    assert all(isinstance(b, Board) for b in boards)
    assert boards[0].id == 395
    assert boards[0].name == "Kanban"
    assert boards[0].type == "kanban"
    assert boards[1].id == 394
    assert boards[1].type == "scrum"


# ---------------------------------------------------------------------------
# list_worklogs
# ---------------------------------------------------------------------------


@respx.mock
def test_list_worklogs_empty(client: JiraClient) -> None:
    fixture = _load("get-worklog-rlm3.json")
    respx.get(f"{BASE_URL}/rest/api/2/issue/RLM-3/worklog").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    result = client.list_worklogs("RLM-3")

    assert isinstance(result, WorklogList)
    assert result.worklogs == []


# ---------------------------------------------------------------------------
# list_watchers
# ---------------------------------------------------------------------------


@respx.mock
def test_list_watchers(client: JiraClient) -> None:
    fixture = _load("get-watchers-rlm3.json")
    respx.get(f"{BASE_URL}/rest/api/2/issue/RLM-3/watchers").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    result = client.list_watchers("RLM-3")

    assert isinstance(result, WatcherList)
    assert result.watcher_count == 1
    assert result.is_watching is True
    assert len(result.watchers) == 1
    assert result.watchers[0].name == "eunsan.jo"


# ---------------------------------------------------------------------------
# preprocessing — replace_mentions
# ---------------------------------------------------------------------------


def test_replace_mentions_basic() -> None:
    assert replace_mentions("[~accountid:abc123]") == "@user-abc123"


def test_replace_mentions_multiple() -> None:
    text = "cc [~accountid:alice] and [~accountid:bob]"
    assert replace_mentions(text) == "cc @user-alice and @user-bob"


def test_replace_mentions_no_match() -> None:
    text = "no mentions here"
    assert replace_mentions(text) == text


# ---------------------------------------------------------------------------
# preprocessing — normalize_smart_links
# ---------------------------------------------------------------------------


def test_normalize_smart_links_basic() -> None:
    assert normalize_smart_links("[Go here|http://x.com|smart-link]") == "[Go here|http://x.com]"


def test_normalize_smart_links_case_insensitive() -> None:
    assert normalize_smart_links("[Go|http://x|Smart-Link]") == "[Go|http://x]"


def test_normalize_smart_links_no_match() -> None:
    text = "[normal link|http://x.com]"
    assert normalize_smart_links(text) == text


def test_normalize_smart_links_multiple() -> None:
    text = "[A|http://a|smart-link] and [B|http://b|smart-link]"
    result = normalize_smart_links(text)
    assert result == "[A|http://a] and [B|http://b]"


# ---------------------------------------------------------------------------
# preprocess_jira_text — combined
# ---------------------------------------------------------------------------


def test_preprocess_jira_text_combined() -> None:
    text = "cc [~accountid:user1] see [Link|http://x|smart-link]"
    result = preprocess_jira_text(text)
    assert result == "cc @user-user1 see [Link|http://x]"


# ---------------------------------------------------------------------------
# search — explicit fields, empty results, maxResults cap
# ---------------------------------------------------------------------------


@respx.mock
def test_search_with_explicit_fields(client: JiraClient) -> None:
    fixture = _load("search-rlm.json")
    route = respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    client.search("project=RLM", fields=["summary", "status", "assignee"])

    req = route.calls[0].request
    assert "summary" in req.url.params["fields"]
    assert "status" in req.url.params["fields"]


@respx.mock
def test_search_empty_results(client: JiraClient) -> None:
    empty = {"total": 0, "start_at": 0, "max_results": 50, "issues": []}
    respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        return_value=httpx.Response(200, json=empty)
    )

    result = client.search("project=NONEXISTENT")

    assert isinstance(result, SearchResult)
    assert result.total == 0
    assert result.issues == []


@respx.mock
def test_search_max_results_capping(client: JiraClient) -> None:
    fixture = _load("search-rlm.json")
    route = respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    client.search("project=RLM", max_results=10)

    req = route.calls[0].request
    assert req.url.params["maxResults"] == "10"


# ---------------------------------------------------------------------------
# get_issue — 404 → NotFoundError, 401 → AuthError
# ---------------------------------------------------------------------------


@respx.mock
def test_get_issue_404_raises_not_found_error(client: JiraClient) -> None:
    from atlassian_skills.core.errors import NotFoundError

    respx.get(f"{BASE_URL}/rest/api/2/issue/RLM-9999").mock(
        return_value=httpx.Response(404, text="Issue does not exist")
    )

    with pytest.raises(NotFoundError):
        client.get_issue("RLM-9999")


@respx.mock
def test_get_issue_401_raises_auth_error(client: JiraClient) -> None:
    from atlassian_skills.core.errors import AuthError

    respx.get(f"{BASE_URL}/rest/api/2/issue/RLM-1").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )

    with pytest.raises(AuthError):
        client.get_issue("RLM-1")


# ---------------------------------------------------------------------------
# list_boards — empty response, HTTP error
# ---------------------------------------------------------------------------


@respx.mock
def test_list_boards_empty_response(client: JiraClient) -> None:
    respx.get(f"{BASE_URL}/rest/agile/1.0/board").mock(
        return_value=httpx.Response(200, json={"values": []})
    )

    boards = client.list_boards()

    assert boards == []


@respx.mock
def test_list_boards_http_error_raises(client: JiraClient) -> None:
    from atlassian_skills.core.errors import AuthError

    respx.get(f"{BASE_URL}/rest/agile/1.0/board").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )

    with pytest.raises(AuthError):
        client.list_boards()


# ---------------------------------------------------------------------------
# list_sprints — with state filter
# ---------------------------------------------------------------------------


@respx.mock
def test_list_sprints_with_state_filter(client: JiraClient) -> None:
    resp_json = {
        "values": [
            {"id": 1, "name": "Active Sprint", "state": "active"},
        ]
    }
    route = respx.get(f"{BASE_URL}/rest/agile/1.0/board/10/sprint").mock(
        return_value=httpx.Response(200, json=resp_json)
    )

    sprints = client.list_sprints(10, state="active")

    req = route.calls[0].request
    assert req.url.params["state"] == "active"
    assert len(sprints) == 1
    assert sprints[0].state == "active"


@respx.mock
def test_list_sprints_no_state_filter_omits_param(client: JiraClient) -> None:
    route = respx.get(f"{BASE_URL}/rest/agile/1.0/board/10/sprint").mock(
        return_value=httpx.Response(200, json={"values": []})
    )

    client.list_sprints(10)

    req = route.calls[0].request
    assert "state" not in req.url.params


# ---------------------------------------------------------------------------
# search_fields — no keyword → returns all, keyword match fuzzy
# ---------------------------------------------------------------------------


@respx.mock
def test_search_fields_returns_all_when_no_keyword(client: JiraClient) -> None:
    fixture = _load("search-fields-epic.json")
    respx.get(f"{BASE_URL}/rest/api/2/field").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    fields = client.search_fields()

    assert len(fields) > 0
    assert all(isinstance(f, JiraField) for f in fields)


@respx.mock
def test_search_fields_keyword_partial_match(client: JiraClient) -> None:
    fixture = _load("search-fields-epic.json")
    respx.get(f"{BASE_URL}/rest/api/2/field").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    # "epic" should match fields whose name or id contains "epic"
    fields = client.search_fields(keyword="epic")

    assert len(fields) >= 1
    assert all("epic" in f.name.lower() or "epic" in f.id.lower() for f in fields)


@respx.mock
def test_search_fields_keyword_no_match(client: JiraClient) -> None:
    fixture = _load("search-fields-epic.json")
    respx.get(f"{BASE_URL}/rest/api/2/field").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    fields = client.search_fields(keyword="zzz_no_such_field")

    assert fields == []


# ---------------------------------------------------------------------------
# get_project_versions — empty list
# ---------------------------------------------------------------------------


@respx.mock
def test_get_project_versions_empty(client: JiraClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/2/project/RLM/versions").mock(
        return_value=httpx.Response(200, json=[])
    )

    versions = client.get_project_versions("RLM")

    assert versions == []


@respx.mock
def test_get_project_versions_returns_list(client: JiraClient) -> None:
    data = [{"id": "10001", "name": "1.0.0", "released": False, "archived": False}]
    respx.get(f"{BASE_URL}/rest/api/2/project/RLM/versions").mock(
        return_value=httpx.Response(200, json=data)
    )

    versions = client.get_project_versions("RLM")

    assert len(versions) == 1
    assert versions[0].name == "1.0.0"


# ---------------------------------------------------------------------------
# get_project_components — empty list
# ---------------------------------------------------------------------------


@respx.mock
def test_get_project_components_empty(client: JiraClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/2/project/RLM/components").mock(
        return_value=httpx.Response(200, json=[])
    )

    components = client.get_project_components("RLM")

    assert components == []


# ---------------------------------------------------------------------------
# list_watchers — missing watchers key fallback
# ---------------------------------------------------------------------------


@respx.mock
def test_list_watchers_missing_watchers_key(client: JiraClient) -> None:
    # Response without "watchers" key — fallback to empty list
    data = {"watchCount": 0, "isWatching": False}
    respx.get(f"{BASE_URL}/rest/api/2/issue/RLM-3/watchers").mock(
        return_value=httpx.Response(200, json=data)
    )

    result = client.list_watchers("RLM-3")

    assert isinstance(result, WatcherList)
    assert result.watcher_count == 0
    assert result.watchers == []


# ---------------------------------------------------------------------------
# get_issue_images
# ---------------------------------------------------------------------------


@respx.mock
def test_get_issue_images_returns_only_images(client: JiraClient) -> None:
    data = {
        "id": "629816",
        "key": "RLM-3",
        "fields": {
            "attachment": [
                {"id": "1", "filename": "photo.jpg", "mimeType": "image/jpeg"},
                {"id": "2", "filename": "doc.pdf", "mimeType": "application/pdf"},
                {"id": "3", "filename": "chart.png", "mimeType": "image/png"},
            ]
        },
    }
    respx.get(f"{BASE_URL}/rest/api/2/issue/RLM-3").mock(
        return_value=httpx.Response(200, json=data)
    )

    images = client.get_issue_images("RLM-3")

    assert len(images) == 2
    filenames = {img["filename"] for img in images}
    assert "photo.jpg" in filenames
    assert "chart.png" in filenames
    assert "doc.pdf" not in filenames


@respx.mock
def test_get_issue_images_no_attachments(client: JiraClient) -> None:
    data = {"id": "1", "key": "RLM-1", "fields": {"attachment": []}}
    respx.get(f"{BASE_URL}/rest/api/2/issue/RLM-1").mock(
        return_value=httpx.Response(200, json=data)
    )

    images = client.get_issue_images("RLM-1")

    assert images == []


# ---------------------------------------------------------------------------
# get_issue_dates
# ---------------------------------------------------------------------------


@respx.mock
def test_get_issue_dates_returns_all_date_fields(client: JiraClient) -> None:
    data = {
        "id": "629816",
        "key": "RLM-3",
        "fields": {
            "created": "2024-01-01T10:00:00.000+0000",
            "updated": "2024-06-01T12:00:00.000+0000",
            "duedate": "2024-07-01",
            "resolutiondate": None,
        },
    }
    respx.get(f"{BASE_URL}/rest/api/2/issue/RLM-3").mock(
        return_value=httpx.Response(200, json=data)
    )

    dates = client.get_issue_dates("RLM-3")

    assert dates["key"] == "RLM-3"
    assert dates["created"] == "2024-01-01T10:00:00.000+0000"
    assert dates["updated"] == "2024-06-01T12:00:00.000+0000"
    assert dates["due_date"] == "2024-07-01"
    assert dates["resolution_date"] is None


@respx.mock
def test_get_issue_dates_sends_correct_fields_param(client: JiraClient) -> None:
    data = {
        "id": "629816",
        "key": "RLM-3",
        "fields": {"created": "2024-01-01", "updated": "2024-06-01", "duedate": None, "resolutiondate": None},
    }
    route = respx.get(f"{BASE_URL}/rest/api/2/issue/RLM-3").mock(
        return_value=httpx.Response(200, json=data)
    )

    client.get_issue_dates("RLM-3")

    req = route.calls[0].request
    assert "duedate" in req.url.params["fields"]
    assert "created" in req.url.params["fields"]


# ---------------------------------------------------------------------------
# get_issue_sla
# ---------------------------------------------------------------------------


@respx.mock
def test_get_issue_sla_returns_dict(client: JiraClient) -> None:
    sla_data = {
        "values": [
            {"id": 1, "name": "Time to first response", "ongoingCycle": {"breached": False}},
            {"id": 2, "name": "Time to resolution", "ongoingCycle": {"breached": True}},
        ]
    }
    respx.get(f"{BASE_URL}/rest/servicedeskapi/request/RLM-3/sla").mock(
        return_value=httpx.Response(200, json=sla_data)
    )

    result = client.get_issue_sla("RLM-3")

    assert isinstance(result, dict)
    assert "values" in result
    assert len(result["values"]) == 2
    assert result["values"][0]["name"] == "Time to first response"


# ---------------------------------------------------------------------------
# search_fields — additional coverage
# ---------------------------------------------------------------------------


@respx.mock
def test_search_fields_with_keyword_filter(client: JiraClient) -> None:
    fixture = _load("search-fields-epic.json")
    respx.get(f"{BASE_URL}/rest/api/2/field").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    fields = client.search_fields(keyword="epic link")

    assert len(fields) == 1
    assert fields[0].name == "Epic Link"


# ---------------------------------------------------------------------------
# get_field_options
# ---------------------------------------------------------------------------


@respx.mock
def test_get_field_options_returns_allowed_values(client: JiraClient) -> None:
    createmeta = {
        "projects": [
            {
                "key": "RLM",
                "issuetypes": [
                    {
                        "name": "Bug",
                        "fields": {
                            "priority": {
                                "allowedValues": [
                                    {"id": "1", "name": "Highest"},
                                    {"id": "2", "name": "High"},
                                    {"id": "3", "name": "Medium"},
                                ]
                            }
                        },
                    }
                ],
            }
        ]
    }
    respx.get(f"{BASE_URL}/rest/api/2/issue/createmeta").mock(
        return_value=httpx.Response(200, json=createmeta)
    )

    options = client.get_field_options("priority", "RLM", "Bug")

    assert len(options) == 3
    assert options[0]["name"] == "Highest"


@respx.mock
def test_get_field_options_field_not_found_returns_empty(client: JiraClient) -> None:
    createmeta = {
        "projects": [
            {
                "key": "RLM",
                "issuetypes": [{"name": "Task", "fields": {}}],
            }
        ]
    }
    respx.get(f"{BASE_URL}/rest/api/2/issue/createmeta").mock(
        return_value=httpx.Response(200, json=createmeta)
    )

    options = client.get_field_options("nonexistent_field", "RLM", "Task")

    assert options == []


# ---------------------------------------------------------------------------
# get_dev_info
# ---------------------------------------------------------------------------


@respx.mock
def test_get_dev_info_returns_dict(client: JiraClient) -> None:
    dev_data = {
        "detail": [
            {
                "repositories": [
                    {"name": "my-repo", "url": "https://bitbucket.example.com/my-repo", "commits": []}
                ]
            }
        ]
    }
    route = respx.get(f"{BASE_URL}/rest/dev-status/1.0/issue/detail").mock(
        return_value=httpx.Response(200, json=dev_data)
    )

    result = client.get_dev_info("629816")

    req = route.calls[0].request
    assert req.url.params["issueId"] == "629816"
    assert req.url.params["applicationType"] == "stash"
    assert req.url.params["dataType"] == "repository"
    assert "detail" in result
    assert result["detail"][0]["repositories"][0]["name"] == "my-repo"


# ---------------------------------------------------------------------------
# list_link_types
# ---------------------------------------------------------------------------


@respx.mock
def test_list_link_types_returns_list(client: JiraClient) -> None:
    from atlassian_skills.jira.models import LinkType

    data = {
        "issueLinkTypes": [
            {"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
            {"id": "10001", "name": "Cloners", "inward": "is cloned by", "outward": "clones"},
            {"id": "10003", "name": "Relates", "inward": "relates to", "outward": "relates to"},
        ]
    }
    respx.get(f"{BASE_URL}/rest/api/2/issueLinkType").mock(
        return_value=httpx.Response(200, json=data)
    )

    link_types = client.list_link_types()

    assert len(link_types) == 3
    assert all(isinstance(lt, LinkType) for lt in link_types)
    assert link_types[0].name == "Blocks"
    assert link_types[0].outward == "blocks"


@respx.mock
def test_list_link_types_plain_list_response(client: JiraClient) -> None:
    from atlassian_skills.jira.models import LinkType

    data = [
        {"id": "1", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
    ]
    respx.get(f"{BASE_URL}/rest/api/2/issueLinkType").mock(
        return_value=httpx.Response(200, json=data)
    )

    link_types = client.list_link_types()

    assert len(link_types) == 1
    assert isinstance(link_types[0], LinkType)


# ---------------------------------------------------------------------------
# list_worklogs — fixture-based
# ---------------------------------------------------------------------------


@respx.mock
def test_list_worklogs_fixture(client: JiraClient) -> None:
    fixture = _load("get-worklog-rlm3.json")
    respx.get(f"{BASE_URL}/rest/api/2/issue/RLM-3/worklog").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    result = client.list_worklogs("RLM-3")

    assert isinstance(result, WorklogList)
    assert result.worklogs == []


@respx.mock
def test_list_worklogs_with_entries(client: JiraClient) -> None:
    data = {
        "worklogs": [
            {
                "id": "10001",
                "comment": "Worked on bug fix",
                "time_spent_seconds": 3600,
                "time_spent": "1h",
                "started": "2024-01-01T09:00:00.000+0000",
                "author": {"displayName": "Alice", "name": "alice"},
            }
        ]
    }
    respx.get(f"{BASE_URL}/rest/api/2/issue/RLM-5/worklog").mock(
        return_value=httpx.Response(200, json=data)
    )

    result = client.list_worklogs("RLM-5")

    assert len(result.worklogs) == 1
    assert result.worklogs[0].comment == "Worked on bug fix"
    assert result.worklogs[0].time_spent_seconds == 3600


# ---------------------------------------------------------------------------
# list_watchers — fixture-based
# ---------------------------------------------------------------------------


@respx.mock
def test_list_watchers_fixture(client: JiraClient) -> None:
    fixture = _load("get-watchers-rlm3.json")
    respx.get(f"{BASE_URL}/rest/api/2/issue/RLM-3/watchers").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    result = client.list_watchers("RLM-3")

    assert isinstance(result, WatcherList)
    assert result.watcher_count == 1
    assert result.is_watching is True
    assert result.watchers[0].name == "eunsan.jo"


# ---------------------------------------------------------------------------
# list_boards — with type filter
# ---------------------------------------------------------------------------


@respx.mock
def test_list_boards_with_type_filter(client: JiraClient) -> None:
    data = {"values": [{"id": "394", "name": "Scrum Board", "type": "scrum"}]}
    route = respx.get(f"{BASE_URL}/rest/agile/1.0/board").mock(
        return_value=httpx.Response(200, json=data)
    )

    boards = client.list_boards(board_type="scrum")

    req = route.calls[0].request
    assert req.url.params["type"] == "scrum"
    assert len(boards) == 1
    assert boards[0].type == "scrum"


# ---------------------------------------------------------------------------
# get_board_issues
# ---------------------------------------------------------------------------


@respx.mock
def test_get_board_issues_returns_issues(client: JiraClient) -> None:
    data = {
        "issues": [
            {"id": "100", "key": "RLM-10", "fields": {"summary": "Issue 10", "status": {"name": "To Do"}, "issuetype": {"name": "Task"}}},
            {"id": "101", "key": "RLM-11", "fields": {"summary": "Issue 11", "status": {"name": "In Progress"}, "issuetype": {"name": "Story"}}},
        ]
    }
    respx.get(f"{BASE_URL}/rest/agile/1.0/board/394/issue").mock(
        return_value=httpx.Response(200, json=data)
    )

    issues = client.get_board_issues(394)

    assert len(issues) == 2
    assert all(isinstance(i, Issue) for i in issues)
    assert issues[0].key == "RLM-10"
    assert issues[1].key == "RLM-11"


@respx.mock
def test_get_board_issues_with_jql_filter(client: JiraClient) -> None:
    data = {"issues": [{"id": "100", "key": "RLM-10", "fields": {"summary": "s", "issuetype": {"name": "Task"}}}]}
    route = respx.get(f"{BASE_URL}/rest/agile/1.0/board/394/issue").mock(
        return_value=httpx.Response(200, json=data)
    )

    client.get_board_issues(394, jql="status=Done")

    req = route.calls[0].request
    assert req.url.params["jql"] == "status=Done"


# ---------------------------------------------------------------------------
# list_sprints — with state filter using fixture
# ---------------------------------------------------------------------------


@respx.mock
def test_list_sprints_with_state_fixture(client: JiraClient) -> None:
    fixture = _load("get-sprints-sample.json")
    route = respx.get(f"{BASE_URL}/rest/agile/1.0/board/395/sprint").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    from atlassian_skills.jira.models import Sprint

    sprints = client.list_sprints(395, state="active")

    req = route.calls[0].request
    assert req.url.params["state"] == "active"
    assert len(sprints) == 2
    assert all(isinstance(s, Sprint) for s in sprints)
    assert sprints[0].name == "Sprint 1"
    assert sprints[0].state == "active"


# ---------------------------------------------------------------------------
# get_sprint_issues
# ---------------------------------------------------------------------------


@respx.mock
def test_get_sprint_issues_returns_search_result(client: JiraClient) -> None:
    data = {
        "total": 2,
        "startAt": 0,
        "maxResults": 50,
        "issues": [
            {"id": "1", "key": "RLM-1", "fields": {"summary": "Task 1", "issuetype": {"name": "Task"}}},
            {"id": "2", "key": "RLM-2", "fields": {"summary": "Task 2", "issuetype": {"name": "Story"}}},
        ],
    }
    route = respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        return_value=httpx.Response(200, json=data)
    )

    result = client.get_sprint_issues(42)

    req = route.calls[0].request
    assert 'sprint="42"' in req.url.params["jql"]
    assert isinstance(result, SearchResult)
    assert result.total == 2
    assert len(result.issues) == 2


# ---------------------------------------------------------------------------
# list_service_desks
# ---------------------------------------------------------------------------


@respx.mock
def test_list_service_desks_returns_list(client: JiraClient) -> None:
    data = {
        "values": [
            {"id": "1", "projectId": "10001", "projectName": "IT Help Desk", "projectKey": "IT"},
            {"id": "2", "projectId": "10002", "projectName": "HR Service", "projectKey": "HR"},
        ]
    }
    respx.get(f"{BASE_URL}/rest/servicedeskapi/servicedesk").mock(
        return_value=httpx.Response(200, json=data)
    )

    result = client.list_service_desks()

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["projectKey"] == "IT"


@respx.mock
def test_list_service_desks_plain_list_response(client: JiraClient) -> None:
    data = [{"id": "1", "projectKey": "IT"}]
    respx.get(f"{BASE_URL}/rest/servicedeskapi/servicedesk").mock(
        return_value=httpx.Response(200, json=data)
    )

    result = client.list_service_desks()

    assert len(result) == 1


# ---------------------------------------------------------------------------
# get_service_desk_queues
# ---------------------------------------------------------------------------


@respx.mock
def test_get_service_desk_queues_returns_list(client: JiraClient) -> None:
    data = {
        "values": [
            {"id": "10", "name": "Urgent Issues", "issueCount": 3},
            {"id": "11", "name": "Open Issues", "issueCount": 15},
        ]
    }
    respx.get(f"{BASE_URL}/rest/servicedeskapi/servicedesk/1/queue").mock(
        return_value=httpx.Response(200, json=data)
    )

    result = client.get_service_desk_queues(1)

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["name"] == "Urgent Issues"
    assert result[1]["issueCount"] == 15
