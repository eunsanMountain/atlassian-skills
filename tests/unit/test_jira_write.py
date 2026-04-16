from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from atlassian_skills.core.auth import Credential
from atlassian_skills.jira.client import JiraClient

FIXTURES = Path(__file__).parent.parent / "fixtures" / "jira"
BASE_URL = "https://jira.example.com"


def _load(name: str) -> dict[str, Any] | list[Any]:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def cred() -> Credential:
    return Credential(method="pat", token="test-token")


@pytest.fixture
def client(cred: Credential) -> JiraClient:
    return JiraClient(BASE_URL, cred)


# ---------------------------------------------------------------------------
# create_issue
# ---------------------------------------------------------------------------


@respx.mock
def test_create_issue(client: JiraClient) -> None:
    fixture = _load("create-issue-expected.json")
    respx.post(f"{BASE_URL}/rest/api/2/issue").mock(return_value=httpx.Response(201, json=fixture))

    result = client.create_issue({"project": {"key": "PROJ"}, "summary": "Test", "issuetype": {"name": "Task"}})

    assert result["key"] == "PROJ-99"
    assert result["id"] == "999999"


# ---------------------------------------------------------------------------
# update_issue
# ---------------------------------------------------------------------------


@respx.mock
def test_update_issue(client: JiraClient) -> None:
    respx.put(f"{BASE_URL}/rest/api/2/issue/PROJ-99").mock(return_value=httpx.Response(204))

    result = client.update_issue("PROJ-99", fields={"summary": "Updated"})

    assert result is None


@respx.mock
def test_update_issue_with_json_response(client: JiraClient) -> None:
    respx.put(f"{BASE_URL}/rest/api/2/issue/PROJ-99").mock(
        return_value=httpx.Response(200, json={"key": "PROJ-99", "fields": {"summary": "Updated"}})
    )

    result = client.update_issue("PROJ-99", fields={"summary": "Updated"})

    assert result is not None
    assert result["key"] == "PROJ-99"


# ---------------------------------------------------------------------------
# delete_issue
# ---------------------------------------------------------------------------


@respx.mock
def test_delete_issue(client: JiraClient) -> None:
    respx.delete(f"{BASE_URL}/rest/api/2/issue/PROJ-99").mock(return_value=httpx.Response(204))

    result = client.delete_issue("PROJ-99")

    assert result is None


# ---------------------------------------------------------------------------
# transition_issue
# ---------------------------------------------------------------------------


@respx.mock
def test_transition_issue(client: JiraClient) -> None:
    respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-3/transitions").mock(return_value=httpx.Response(204))

    client.transition_issue("PROJ-3", "21")


@respx.mock
def test_transition_issue_with_comment(client: JiraClient) -> None:
    route = respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-3/transitions").mock(return_value=httpx.Response(204))

    client.transition_issue("PROJ-3", "21", comment="Moving to done")

    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["transition"]["id"] == "21"
    assert body["update"]["comment"][0]["add"]["body"] == "Moving to done"


# ---------------------------------------------------------------------------
# add_comment
# ---------------------------------------------------------------------------


@respx.mock
def test_add_comment(client: JiraClient) -> None:
    resp_json = {"id": "12345", "body": "Test comment", "author": {"name": "user1"}}
    respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-3/comment").mock(return_value=httpx.Response(201, json=resp_json))

    result = client.add_comment("PROJ-3", "Test comment")

    assert result["id"] == "12345"
    assert result["body"] == "Test comment"


# ---------------------------------------------------------------------------
# edit_comment
# ---------------------------------------------------------------------------


@respx.mock
def test_edit_comment(client: JiraClient) -> None:
    resp_json = {"id": "12345", "body": "Edited comment", "author": {"name": "user1"}}
    respx.put(f"{BASE_URL}/rest/api/2/issue/PROJ-3/comment/12345").mock(
        return_value=httpx.Response(200, json=resp_json)
    )

    result = client.edit_comment("PROJ-3", "12345", "Edited comment")

    assert result["body"] == "Edited comment"


# ---------------------------------------------------------------------------
# delete_comment
# ---------------------------------------------------------------------------


@respx.mock
def test_delete_comment(client: JiraClient) -> None:
    respx.delete(f"{BASE_URL}/rest/api/2/issue/TEST-1/comment/12345").mock(return_value=httpx.Response(204))

    result = client.delete_comment("TEST-1", "12345")

    assert result is None


# ---------------------------------------------------------------------------
# create_issue_link
# ---------------------------------------------------------------------------


@respx.mock
def test_create_issue_link(client: JiraClient) -> None:
    resp_json = {"id": "100", "type": {"name": "Blocks"}}
    respx.post(f"{BASE_URL}/rest/api/2/issueLink").mock(return_value=httpx.Response(201, json=resp_json))

    result = client.create_issue_link("Blocks", "PROJ-1", "PROJ-2")

    assert result is not None
    assert result["id"] == "100"


@respx.mock
def test_create_issue_link_204(client: JiraClient) -> None:
    respx.post(f"{BASE_URL}/rest/api/2/issueLink").mock(return_value=httpx.Response(204))

    result = client.create_issue_link("Blocks", "PROJ-1", "PROJ-2")

    assert result is None


# ---------------------------------------------------------------------------
# add_watcher
# ---------------------------------------------------------------------------


@respx.mock
def test_add_watcher(client: JiraClient) -> None:
    respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-3/watchers").mock(return_value=httpx.Response(204))

    client.add_watcher("PROJ-3", "testuser")


# ---------------------------------------------------------------------------
# remove_watcher
# ---------------------------------------------------------------------------


@respx.mock
def test_remove_watcher(client: JiraClient) -> None:
    respx.delete(url__regex=r".*/rest/api/2/issue/PROJ-3/watchers.*").mock(return_value=httpx.Response(204))

    client.remove_watcher("PROJ-3", "testuser")


# ---------------------------------------------------------------------------
# create_sprint
# ---------------------------------------------------------------------------


@respx.mock
def test_create_sprint(client: JiraClient) -> None:
    resp_json = {"id": 42, "name": "Sprint 1", "state": "future", "originBoardId": 10}
    respx.post(f"{BASE_URL}/rest/agile/1.0/sprint").mock(return_value=httpx.Response(201, json=resp_json))

    result = client.create_sprint("Sprint 1", 10, goal="Ship it")

    assert result["id"] == 42
    assert result["name"] == "Sprint 1"


# ---------------------------------------------------------------------------
# upload_attachment
# ---------------------------------------------------------------------------


@respx.mock
def test_upload_attachment(client: JiraClient) -> None:
    resp_json = [{"id": "55", "filename": "test.txt", "size": 4}]
    respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-3/attachments").mock(return_value=httpx.Response(200, json=resp_json))

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"test")
        f.flush()
        result = client.upload_attachment("PROJ-3", f.name)

    assert result[0]["id"] == "55"


# ---------------------------------------------------------------------------
# add_worklog
# ---------------------------------------------------------------------------


@respx.mock
def test_add_worklog(client: JiraClient) -> None:
    resp_json = {"id": "777", "timeSpent": "1h", "timeSpentSeconds": 3600}
    respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-3/worklog").mock(return_value=httpx.Response(201, json=resp_json))

    result = client.add_worklog("PROJ-3", 3600, comment="Working on it")

    assert result["id"] == "777"
    assert result["timeSpentSeconds"] == 3600


# ---------------------------------------------------------------------------
# delete_attachment
# ---------------------------------------------------------------------------


@respx.mock
def test_delete_attachment(client: JiraClient) -> None:
    respx.delete(f"{BASE_URL}/rest/api/2/attachment/55").mock(return_value=httpx.Response(204))

    client.delete_attachment("55")


# ---------------------------------------------------------------------------
# remove_issue_link
# ---------------------------------------------------------------------------


@respx.mock
def test_remove_issue_link(client: JiraClient) -> None:
    respx.delete(f"{BASE_URL}/rest/api/2/issueLink/100").mock(return_value=httpx.Response(204))

    client.remove_issue_link("100")


# ---------------------------------------------------------------------------
# add_issues_to_sprint
# ---------------------------------------------------------------------------


@respx.mock
def test_add_issues_to_sprint(client: JiraClient) -> None:
    respx.post(f"{BASE_URL}/rest/agile/1.0/sprint/42/issue").mock(return_value=httpx.Response(204))

    client.add_issues_to_sprint(42, ["PROJ-1", "PROJ-2"])


# ---------------------------------------------------------------------------
# create_version
# ---------------------------------------------------------------------------


@respx.mock
def test_create_version(client: JiraClient) -> None:
    resp_json = {"id": "10100", "name": "1.0.0", "project": "PROJ"}
    respx.post(f"{BASE_URL}/rest/api/2/version").mock(return_value=httpx.Response(201, json=resp_json))

    result = client.create_version("PROJ", "1.0.0", description="First release")

    assert result["id"] == "10100"
    assert result["name"] == "1.0.0"


# ---------------------------------------------------------------------------
# update_sprint
# ---------------------------------------------------------------------------


@respx.mock
def test_update_sprint(client: JiraClient) -> None:
    resp_json = {"id": 42, "name": "Sprint 1 - renamed", "state": "active"}
    respx.put(f"{BASE_URL}/rest/agile/1.0/sprint/42").mock(return_value=httpx.Response(200, json=resp_json))

    result = client.update_sprint(42, name="Sprint 1 - renamed", state="active")

    assert result["name"] == "Sprint 1 - renamed"
    assert result["state"] == "active"


# ---------------------------------------------------------------------------
# list_remote_issue_links
# ---------------------------------------------------------------------------


@respx.mock
def test_list_remote_issue_links(client: JiraClient) -> None:
    resp_json = [
        {"id": 10, "object": {"url": "https://example.com/a", "title": "A"}},
        {"id": 11, "object": {"url": "https://example.com/b", "title": "B"}},
    ]
    respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-3/remotelink").mock(return_value=httpx.Response(200, json=resp_json))

    result = client.list_remote_issue_links("PROJ-3")

    assert len(result) == 2
    assert result[0]["id"] == 10


@respx.mock
def test_list_remote_issue_links_empty(client: JiraClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-9/remotelink").mock(return_value=httpx.Response(200, json=[]))

    assert client.list_remote_issue_links("PROJ-9") == []


# ---------------------------------------------------------------------------
# create_remote_issue_link
# ---------------------------------------------------------------------------


@respx.mock
def test_create_remote_issue_link(client: JiraClient) -> None:
    resp_json = {"id": 200, "self": "https://jira.example.com/rest/api/2/issue/PROJ-3/remotelink/200"}
    respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-3/remotelink").mock(return_value=httpx.Response(201, json=resp_json))

    result = client.create_remote_issue_link("PROJ-3", "https://example.com", "Example", relationship="relates to")

    assert result["id"] == 200


# ---------------------------------------------------------------------------
# batch_create_issues
# ---------------------------------------------------------------------------


@respx.mock
def test_batch_create_issues(client: JiraClient) -> None:
    resp_json = {"issues": [{"id": "1", "key": "PROJ-1"}, {"id": "2", "key": "PROJ-2"}], "errors": []}
    respx.post(f"{BASE_URL}/rest/api/2/issue/bulk").mock(return_value=httpx.Response(201, json=resp_json))

    result = client.batch_create_issues(
        [
            {"project": {"key": "PROJ"}, "summary": "Issue 1", "issuetype": {"name": "Task"}},
            {"project": {"key": "PROJ"}, "summary": "Issue 2", "issuetype": {"name": "Task"}},
        ]
    )

    assert len(result["issues"]) == 2
    assert result["issues"][0]["key"] == "PROJ-1"


# ---------------------------------------------------------------------------
# link_to_epic
# ---------------------------------------------------------------------------


@respx.mock
def test_link_to_epic(client: JiraClient) -> None:
    respx.put(f"{BASE_URL}/rest/api/2/issue/PROJ-5").mock(return_value=httpx.Response(204))

    result = client.link_to_epic("PROJ-5", "PROJ-1", "customfield_10014")

    assert result is None


# ---------------------------------------------------------------------------
# transition_issue — edge cases
# ---------------------------------------------------------------------------


@respx.mock
def test_transition_issue_with_fields(client: JiraClient) -> None:
    route = respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-3/transitions").mock(return_value=httpx.Response(204))

    client.transition_issue("PROJ-3", "21", fields={"resolution": {"name": "Fixed"}})

    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["fields"]["resolution"]["name"] == "Fixed"
    assert body["transition"]["id"] == "21"


@respx.mock
def test_transition_issue_with_resolution_field(client: JiraClient) -> None:
    route = respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-3/transitions").mock(return_value=httpx.Response(204))

    client.transition_issue("PROJ-3", "731", fields={"resolution": {"id": "10001"}})

    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["transition"]["id"] == "731"
    assert body["fields"]["resolution"]["id"] == "10001"


@respx.mock
def test_transition_issue_int_id_sent_as_str(client: JiraClient) -> None:
    """transition_id is passed as str; the client sends it as-is."""
    route = respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-3/transitions").mock(return_value=httpx.Response(204))

    # Caller may pass numeric string — verify it reaches the wire unchanged
    client.transition_issue("PROJ-3", "10")

    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["transition"]["id"] == "10"


@respx.mock
def test_transition_issue_with_comment_body_structure(client: JiraClient) -> None:
    route = respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-3/transitions").mock(return_value=httpx.Response(204))

    client.transition_issue("PROJ-3", "21", comment="Closing this issue")

    req = route.calls[0].request
    body = json.loads(req.content)
    comment_add = body["update"]["comment"][0]["add"]
    assert comment_add["body"] == "Closing this issue"


# ---------------------------------------------------------------------------
# add_comment — edge cases
# ---------------------------------------------------------------------------


@respx.mock
def test_add_comment_with_visibility(client: JiraClient) -> None:
    resp_json = {"id": "99", "body": "Restricted comment", "author": {"name": "user1"}}
    route = respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-3/comment").mock(
        return_value=httpx.Response(201, json=resp_json)
    )

    result = client.add_comment(
        "PROJ-3",
        "Restricted comment",
        visibility={"type": "role", "value": "Developers"},
    )

    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["visibility"] == {"type": "role", "value": "Developers"}
    assert result["id"] == "99"


@respx.mock
def test_add_comment_sends_body_field(client: JiraClient) -> None:
    resp_json = {"id": "100", "body": "Hello", "author": {"name": "u"}}
    route = respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-1/comment").mock(
        return_value=httpx.Response(201, json=resp_json)
    )

    client.add_comment("PROJ-1", "Hello")

    req = route.calls[0].request
    body = json.loads(req.content)
    assert "body" in body
    assert body["body"] == "Hello"


# ---------------------------------------------------------------------------
# edit_comment — additional coverage
# ---------------------------------------------------------------------------


@respx.mock
def test_edit_comment_sends_body_field(client: JiraClient) -> None:
    resp_json = {"id": "12345", "body": "New text", "author": {"name": "u"}}
    route = respx.put(f"{BASE_URL}/rest/api/2/issue/PROJ-3/comment/12345").mock(
        return_value=httpx.Response(200, json=resp_json)
    )

    result = client.edit_comment("PROJ-3", "12345", "New text")

    req = route.calls[0].request
    payload = json.loads(req.content)
    assert payload["body"] == "New text"
    assert result["body"] == "New text"


# ---------------------------------------------------------------------------
# delete_comment — 204 response
# ---------------------------------------------------------------------------


@respx.mock
def test_delete_comment_returns_none(client: JiraClient) -> None:
    respx.delete(f"{BASE_URL}/rest/api/2/issue/PROJ-3/comment/99").mock(return_value=httpx.Response(204))

    result = client.delete_comment("PROJ-3", "99")

    assert result is None


# ---------------------------------------------------------------------------
# create_issue_link — missing type raises error
# ---------------------------------------------------------------------------


@respx.mock
def test_create_issue_link_sends_type_name(client: JiraClient) -> None:
    route = respx.post(f"{BASE_URL}/rest/api/2/issueLink").mock(return_value=httpx.Response(201, json={"id": "50"}))

    client.create_issue_link("Relates", "PROJ-1", "PROJ-3")

    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["type"]["name"] == "Relates"
    assert body["inwardIssue"]["key"] == "PROJ-1"
    assert body["outwardIssue"]["key"] == "PROJ-3"


@respx.mock
def test_create_issue_link_http_error_raises(client: JiraClient) -> None:
    from atlassian_skills.core.errors import NotFoundError

    respx.post(f"{BASE_URL}/rest/api/2/issueLink").mock(return_value=httpx.Response(404, text="Not found"))

    with pytest.raises(NotFoundError):
        client.create_issue_link("Blocks", "PROJ-1", "PROJ-99")


# ---------------------------------------------------------------------------
# remove_issue_link — empty id guard (client level)
# ---------------------------------------------------------------------------


@respx.mock
def test_remove_issue_link_sends_correct_id(client: JiraClient) -> None:
    route = respx.delete(f"{BASE_URL}/rest/api/2/issueLink/42").mock(return_value=httpx.Response(204))

    client.remove_issue_link("42")

    assert route.called


# ---------------------------------------------------------------------------
# create_sprint — with dates
# ---------------------------------------------------------------------------


@respx.mock
def test_create_sprint_with_dates(client: JiraClient) -> None:
    resp_json = {
        "id": 55,
        "name": "Sprint 3",
        "state": "future",
        "originBoardId": 10,
        "startDate": "2025-01-01T00:00:00.000Z",
        "endDate": "2025-01-15T00:00:00.000Z",
    }
    route = respx.post(f"{BASE_URL}/rest/agile/1.0/sprint").mock(return_value=httpx.Response(201, json=resp_json))

    result = client.create_sprint(
        "Sprint 3",
        10,
        start_date="2025-01-01T00:00:00.000Z",
        end_date="2025-01-15T00:00:00.000Z",
    )

    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["startDate"] == "2025-01-01T00:00:00.000Z"
    assert body["endDate"] == "2025-01-15T00:00:00.000Z"
    assert result["id"] == 55


@respx.mock
def test_create_sprint_without_optional_fields(client: JiraClient) -> None:
    resp_json = {"id": 56, "name": "Sprint X", "state": "future", "originBoardId": 5}
    route = respx.post(f"{BASE_URL}/rest/agile/1.0/sprint").mock(return_value=httpx.Response(201, json=resp_json))

    client.create_sprint("Sprint X", 5)

    req = route.calls[0].request
    body = json.loads(req.content)
    assert "startDate" not in body
    assert "endDate" not in body
    assert "goal" not in body


# ---------------------------------------------------------------------------
# update_sprint — state validation scenarios
# ---------------------------------------------------------------------------


@respx.mock
def test_update_sprint_closed_state(client: JiraClient) -> None:
    resp_json = {"id": 42, "name": "Sprint 1", "state": "closed"}
    respx.put(f"{BASE_URL}/rest/agile/1.0/sprint/42").mock(return_value=httpx.Response(200, json=resp_json))

    result = client.update_sprint(42, state="closed")

    assert result["state"] == "closed"


@respx.mock
def test_update_sprint_sends_only_provided_fields(client: JiraClient) -> None:
    route = respx.put(f"{BASE_URL}/rest/agile/1.0/sprint/42").mock(
        return_value=httpx.Response(200, json={"id": 42, "name": "Sprint 1"})
    )

    client.update_sprint(42, name="Sprint 1")

    req = route.calls[0].request
    body = json.loads(req.content)
    assert "name" in body
    assert "state" not in body
    assert "startDate" not in body


# ---------------------------------------------------------------------------
# add_watcher / remove_watcher — error paths (404)
# ---------------------------------------------------------------------------


@respx.mock
def test_add_watcher_404_raises(client: JiraClient) -> None:
    from atlassian_skills.core.errors import NotFoundError

    respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-999/watchers").mock(
        return_value=httpx.Response(404, text="Issue not found")
    )

    with pytest.raises(NotFoundError):
        client.add_watcher("PROJ-999", "testuser")


@respx.mock
def test_remove_watcher_404_raises(client: JiraClient) -> None:
    from atlassian_skills.core.errors import NotFoundError

    respx.delete(url__regex=r".*/rest/api/2/issue/PROJ-999/watchers.*").mock(
        return_value=httpx.Response(404, text="Issue not found")
    )

    with pytest.raises(NotFoundError):
        client.remove_watcher("PROJ-999", "testuser")


# ---------------------------------------------------------------------------
# upload_attachment — file not found error
# ---------------------------------------------------------------------------


def test_upload_attachment_file_not_found(client: JiraClient) -> None:
    with pytest.raises(FileNotFoundError):
        client.upload_attachment("PROJ-3", "/nonexistent/path/file.txt")


@respx.mock
def test_upload_attachment_401_raises(client: JiraClient) -> None:
    from atlassian_skills.core.errors import AuthError

    respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-3/attachments").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"data")
        tmp_path = f.name

    with pytest.raises(AuthError):
        client.upload_attachment("PROJ-3", tmp_path)


# ---------------------------------------------------------------------------
# batch_create_issues — request body structure
# ---------------------------------------------------------------------------


@respx.mock
def test_batch_create_issues_sends_issue_updates_payload(client: JiraClient) -> None:
    resp_json = {"issues": [{"id": "1", "key": "PROJ-1"}], "errors": []}
    route = respx.post(f"{BASE_URL}/rest/api/2/issue/bulk").mock(return_value=httpx.Response(201, json=resp_json))

    client.batch_create_issues([{"project": {"key": "PROJ"}, "summary": "Bulk issue", "issuetype": {"name": "Task"}}])

    req = route.calls[0].request
    body = json.loads(req.content)
    assert "issueUpdates" in body
    assert body["issueUpdates"][0]["fields"]["summary"] == "Bulk issue"


# ---------------------------------------------------------------------------
# edit_comment — verifies PUT URL and body
# ---------------------------------------------------------------------------


@respx.mock
def test_edit_comment_uses_put_with_comment_id(client: JiraClient) -> None:
    resp_json = {"id": "999", "body": "Updated body"}
    route = respx.put(f"{BASE_URL}/rest/api/2/issue/PROJ-10/comment/999").mock(
        return_value=httpx.Response(200, json=resp_json)
    )

    result = client.edit_comment("PROJ-10", "999", "Updated body")

    assert route.called
    req = route.calls[0].request
    payload = json.loads(req.content)
    assert payload["body"] == "Updated body"
    assert result["id"] == "999"


# ---------------------------------------------------------------------------
# delete_comment — verifies DELETE URL
# ---------------------------------------------------------------------------


@respx.mock
def test_delete_comment_uses_delete_method(client: JiraClient) -> None:
    route = respx.delete(f"{BASE_URL}/rest/api/2/issue/PROJ-10/comment/888").mock(return_value=httpx.Response(204))

    client.delete_comment("PROJ-10", "888")

    assert route.called


# ---------------------------------------------------------------------------
# add_worklog — with started and comment
# ---------------------------------------------------------------------------


@respx.mock
def test_add_worklog_with_started_and_comment(client: JiraClient) -> None:
    resp_json = {"id": "10001", "timeSpentSeconds": 7200, "comment": "Deep work session"}
    route = respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-3/worklog").mock(
        return_value=httpx.Response(201, json=resp_json)
    )

    result = client.add_worklog(
        "PROJ-3",
        7200,
        comment="Deep work session",
        started="2024-06-01T09:00:00.000+0000",
    )

    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["timeSpentSeconds"] == 7200
    assert body["comment"] == "Deep work session"
    assert body["started"] == "2024-06-01T09:00:00.000+0000"
    assert result["id"] == "10001"


@respx.mock
def test_add_worklog_minimal_only_time_spent(client: JiraClient) -> None:
    resp_json = {"id": "10002", "timeSpentSeconds": 1800}
    route = respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-5/worklog").mock(
        return_value=httpx.Response(201, json=resp_json)
    )

    client.add_worklog("PROJ-5", 1800)

    req = route.calls[0].request
    body = json.loads(req.content)
    assert "comment" not in body
    assert "started" not in body
    assert body["timeSpentSeconds"] == 1800


# ---------------------------------------------------------------------------
# create_issue_link — verifies request body structure
# ---------------------------------------------------------------------------


@respx.mock
def test_create_issue_link_body_structure(client: JiraClient) -> None:
    route = respx.post(f"{BASE_URL}/rest/api/2/issueLink").mock(return_value=httpx.Response(201, json={"id": "200"}))

    client.create_issue_link("Depends", "PROJ-10", "PROJ-20")

    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["type"]["name"] == "Depends"
    assert body["inwardIssue"]["key"] == "PROJ-10"
    assert body["outwardIssue"]["key"] == "PROJ-20"


# ---------------------------------------------------------------------------
# create_remote_issue_link — with relationship
# ---------------------------------------------------------------------------


@respx.mock
def test_create_remote_issue_link_with_relationship(client: JiraClient) -> None:
    resp_json = {"id": 300}
    route = respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-5/remotelink").mock(
        return_value=httpx.Response(201, json=resp_json)
    )

    result = client.create_remote_issue_link("PROJ-5", "https://docs.example.com", "Docs", relationship="wiki page")

    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["object"]["url"] == "https://docs.example.com"
    assert body["object"]["title"] == "Docs"
    assert body["relationship"] == "wiki page"
    assert result["id"] == 300


@respx.mock
def test_create_remote_issue_link_without_relationship(client: JiraClient) -> None:
    resp_json = {"id": 301}
    route = respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-5/remotelink").mock(
        return_value=httpx.Response(201, json=resp_json)
    )

    client.create_remote_issue_link("PROJ-5", "https://wiki.example.com", "Wiki")

    req = route.calls[0].request
    body = json.loads(req.content)
    assert "relationship" not in body


# ---------------------------------------------------------------------------
# remove_issue_link — verifies DELETE endpoint
# ---------------------------------------------------------------------------


@respx.mock
def test_remove_issue_link_calls_delete(client: JiraClient) -> None:
    route = respx.delete(f"{BASE_URL}/rest/api/2/issueLink/999").mock(return_value=httpx.Response(204))

    client.remove_issue_link("999")

    assert route.called


# ---------------------------------------------------------------------------
# link_to_epic — verifies field sent in PUT body
# ---------------------------------------------------------------------------


@respx.mock
def test_link_to_epic_sends_epic_field(client: JiraClient) -> None:
    route = respx.put(f"{BASE_URL}/rest/api/2/issue/PROJ-10").mock(return_value=httpx.Response(204))

    client.link_to_epic("PROJ-10", "PROJ-1", "customfield_10014")

    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["fields"]["customfield_10014"] == "PROJ-1"


# ---------------------------------------------------------------------------
# add_watcher — verifies body is plain string
# ---------------------------------------------------------------------------


@respx.mock
def test_add_watcher_sends_username_as_json_string(client: JiraClient) -> None:
    route = respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-3/watchers").mock(return_value=httpx.Response(204))

    client.add_watcher("PROJ-3", "alice")

    req = route.calls[0].request
    assert json.loads(req.content) == "alice"


# ---------------------------------------------------------------------------
# remove_watcher — verifies username query param
# ---------------------------------------------------------------------------


@respx.mock
def test_remove_watcher_sends_username_param(client: JiraClient) -> None:
    route = respx.delete(url__regex=r".*/rest/api/2/issue/PROJ-3/watchers.*").mock(return_value=httpx.Response(204))

    client.remove_watcher("PROJ-3", "alice")

    req = route.calls[0].request
    assert req.url.params["username"] == "alice"


# ---------------------------------------------------------------------------
# create_sprint — board_id included in payload
# ---------------------------------------------------------------------------


@respx.mock
def test_create_sprint_payload_includes_board_id(client: JiraClient) -> None:
    resp_json = {"id": 77, "name": "Sprint Y", "state": "future", "originBoardId": 20}
    route = respx.post(f"{BASE_URL}/rest/agile/1.0/sprint").mock(return_value=httpx.Response(201, json=resp_json))

    result = client.create_sprint("Sprint Y", 20)

    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["name"] == "Sprint Y"
    assert body["originBoardId"] == 20
    assert result["id"] == 77


# ---------------------------------------------------------------------------
# update_sprint — verifies only provided fields are sent
# ---------------------------------------------------------------------------


@respx.mock
def test_update_sprint_goal_and_dates(client: JiraClient) -> None:
    resp_json = {"id": 10, "name": "Sprint A", "state": "active", "goal": "Ship feature X"}
    route = respx.put(f"{BASE_URL}/rest/agile/1.0/sprint/10").mock(return_value=httpx.Response(200, json=resp_json))

    result = client.update_sprint(
        10,
        goal="Ship feature X",
        start_date="2025-03-01T00:00:00.000Z",
        end_date="2025-03-14T00:00:00.000Z",
    )

    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["goal"] == "Ship feature X"
    assert body["startDate"] == "2025-03-01T00:00:00.000Z"
    assert body["endDate"] == "2025-03-14T00:00:00.000Z"
    assert result["goal"] == "Ship feature X"


# ---------------------------------------------------------------------------
# add_issues_to_sprint — verifies payload body
# ---------------------------------------------------------------------------


@respx.mock
def test_add_issues_to_sprint_sends_issue_keys(client: JiraClient) -> None:
    route = respx.post(f"{BASE_URL}/rest/agile/1.0/sprint/42/issue").mock(return_value=httpx.Response(204))

    client.add_issues_to_sprint(42, ["PROJ-5", "PROJ-6", "PROJ-7"])

    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["issues"] == ["PROJ-5", "PROJ-6", "PROJ-7"]


# ---------------------------------------------------------------------------
# create_version — with optional fields
# ---------------------------------------------------------------------------


@respx.mock
def test_create_version_with_all_fields(client: JiraClient) -> None:
    resp_json = {
        "id": "20001",
        "name": "2.0.0",
        "project": "PROJ",
        "description": "Major release",
        "startDate": "2025-01-01",
        "releaseDate": "2025-06-30",
    }
    route = respx.post(f"{BASE_URL}/rest/api/2/version").mock(return_value=httpx.Response(201, json=resp_json))

    result = client.create_version(
        "PROJ",
        "2.0.0",
        start_date="2025-01-01",
        release_date="2025-06-30",
        description="Major release",
    )

    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["project"] == "PROJ"
    assert body["name"] == "2.0.0"
    assert body["startDate"] == "2025-01-01"
    assert body["releaseDate"] == "2025-06-30"
    assert body["description"] == "Major release"
    assert result["id"] == "20001"


# ---------------------------------------------------------------------------
# batch_create_versions — multiple sequential POSTs
# ---------------------------------------------------------------------------


@respx.mock
def test_batch_create_versions_calls_version_endpoint_per_version(client: JiraClient) -> None:
    route = respx.post(f"{BASE_URL}/rest/api/2/version").mock(
        side_effect=[
            httpx.Response(201, json={"id": "1001", "name": "1.0.0", "project": "PROJ"}),
            httpx.Response(201, json={"id": "1002", "name": "1.1.0", "project": "PROJ"}),
        ]
    )

    results = client.batch_create_versions(
        [
            {"project": "PROJ", "name": "1.0.0"},
            {"project": "PROJ", "name": "1.1.0"},
        ]
    )

    assert route.call_count == 2
    assert len(results) == 2
    assert results[0]["name"] == "1.0.0"
    assert results[1]["name"] == "1.1.0"


@respx.mock
def test_batch_create_versions_empty_list_returns_empty(client: JiraClient) -> None:
    results = client.batch_create_versions([])

    assert results == []
