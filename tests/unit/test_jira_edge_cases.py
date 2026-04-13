from __future__ import annotations

import json
import tempfile
from typing import Any

import httpx
import pytest
import respx

from atlassian_skills.core.auth import Credential
from atlassian_skills.core.errors import (
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from atlassian_skills.jira.client import JiraClient

BASE_URL = "https://jira.example.com"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cred() -> Credential:
    return Credential(method="pat", token="test-token")


@pytest.fixture
def client(cred: Credential) -> JiraClient:
    return JiraClient(BASE_URL, cred)


# ---------------------------------------------------------------------------
# 1. Error responses — get_issue
# ---------------------------------------------------------------------------


@respx.mock
def test_get_issue_404_raises_not_found(client: JiraClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-404").mock(
        return_value=httpx.Response(404, json={"errorMessages": ["Issue does not exist."], "errors": {}})
    )

    with pytest.raises(NotFoundError) as exc_info:
        client.get_issue("PROJ-404")

    assert exc_info.value.http_status == 404


@respx.mock
def test_get_issue_403_raises_forbidden(client: JiraClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(
        return_value=httpx.Response(403, json={"errorMessages": ["You do not have permission."], "errors": {}})
    )

    with pytest.raises(ForbiddenError) as exc_info:
        client.get_issue("PROJ-1")

    assert exc_info.value.http_status == 403


# ---------------------------------------------------------------------------
# 1. Error responses — search
# ---------------------------------------------------------------------------


@respx.mock
def test_search_invalid_jql_400_raises_validation_error(client: JiraClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        return_value=httpx.Response(
            400,
            json={"errorMessages": ["Error in the JQL Query: The field 'badfield' does not exist."], "errors": {}},
        )
    )

    with pytest.raises(ValidationError) as exc_info:
        client.search("badfield = something")

    assert exc_info.value.http_status == 400


# ---------------------------------------------------------------------------
# 1. Error responses — create_issue
# ---------------------------------------------------------------------------


@respx.mock
def test_create_issue_400_raises_validation_error(client: JiraClient) -> None:
    respx.post(f"{BASE_URL}/rest/api/2/issue").mock(
        return_value=httpx.Response(
            400,
            json={"errorMessages": [], "errors": {"summary": "Field 'summary' is required."}},
        )
    )

    with pytest.raises(ValidationError) as exc_info:
        client.create_issue({"project": {"key": "PROJ"}})

    assert exc_info.value.http_status == 400


# ---------------------------------------------------------------------------
# 1. Error responses — delete_issue
# ---------------------------------------------------------------------------


@respx.mock
def test_delete_issue_404_raises_not_found(client: JiraClient) -> None:
    respx.delete(f"{BASE_URL}/rest/api/2/issue/PROJ-999").mock(
        return_value=httpx.Response(404, json={"errorMessages": ["Issue does not exist."], "errors": {}})
    )

    with pytest.raises(NotFoundError) as exc_info:
        client.delete_issue("PROJ-999")

    assert exc_info.value.http_status == 404


# ---------------------------------------------------------------------------
# 2. get_field_options — via createmeta
# ---------------------------------------------------------------------------


@respx.mock
def test_get_field_options_returns_values(client: JiraClient) -> None:
    payload = {
        "projects": [
            {
                "key": "PROJ",
                "issuetypes": [
                    {
                        "name": "Task",
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
    respx.get(f"{BASE_URL}/rest/api/2/issue/createmeta").mock(return_value=httpx.Response(200, json=payload))

    result = client.get_field_options("priority", "PROJ", "Task")

    assert len(result) == 3
    assert result[0]["name"] == "Highest"
    assert result[2]["id"] == "3"


@respx.mock
def test_get_field_options_empty_when_field_not_found(client: JiraClient) -> None:
    payload = {
        "projects": [
            {
                "key": "PROJ",
                "issuetypes": [
                    {
                        "name": "Task",
                        "fields": {},
                    }
                ],
            }
        ]
    }
    respx.get(f"{BASE_URL}/rest/api/2/issue/createmeta").mock(return_value=httpx.Response(200, json=payload))

    result = client.get_field_options("nonexistent_field", "PROJ", "Task")

    assert result == []


@respx.mock
def test_get_field_options_empty_when_no_projects(client: JiraClient) -> None:
    payload: dict[str, Any] = {"projects": []}
    respx.get(f"{BASE_URL}/rest/api/2/issue/createmeta").mock(return_value=httpx.Response(200, json=payload))

    result = client.get_field_options("priority", "PROJ", "Task")

    assert result == []


@respx.mock
def test_get_field_options_passes_project_key_param(client: JiraClient) -> None:
    payload = {
        "projects": [
            {
                "key": "MYPROJ",
                "issuetypes": [
                    {
                        "name": "Bug",
                        "fields": {
                            "customfield_10020": {
                                "allowedValues": [{"id": "10", "value": "Option A"}]
                            }
                        },
                    }
                ],
            }
        ]
    }
    route = respx.get(f"{BASE_URL}/rest/api/2/issue/createmeta").mock(return_value=httpx.Response(200, json=payload))

    result = client.get_field_options("customfield_10020", "MYPROJ", "Bug")

    req = route.calls[0].request
    assert req.url.params["projectKeys"] == "MYPROJ"
    assert req.url.params["issuetypeNames"] == "Bug"
    assert len(result) == 1


# ---------------------------------------------------------------------------
# 3. get_dev_info_many
# ---------------------------------------------------------------------------


@respx.mock
def test_get_dev_info_many_multiple_keys(client: JiraClient) -> None:
    resp_json: dict[str, Any] = {
        "issueOrPullRequestIDs": ["PROJ-1", "PROJ-2", "PROJ-3"],
        "detail": [
            {"issueId": "PROJ-1", "pullRequests": [], "branches": []},
            {"issueId": "PROJ-2", "pullRequests": [{"id": "pr1"}], "branches": []},
            {"issueId": "PROJ-3", "pullRequests": [], "branches": [{"name": "feature-x"}]},
        ],
    }
    route = respx.get(f"{BASE_URL}/rest/dev-status/1.0/issue/summary").mock(
        return_value=httpx.Response(200, json=resp_json)
    )

    result = client.get_dev_info_many(["PROJ-1", "PROJ-2", "PROJ-3"])

    req = route.calls[0].request
    assert "PROJ-1" in req.url.params["issueId"]
    assert "PROJ-2" in req.url.params["issueId"]
    assert "PROJ-3" in req.url.params["issueId"]
    assert len(result["detail"]) == 3


@respx.mock
def test_get_dev_info_many_empty_keys(client: JiraClient) -> None:
    resp_json: dict[str, Any] = {"issueOrPullRequestIDs": [], "detail": []}
    route = respx.get(f"{BASE_URL}/rest/dev-status/1.0/issue/summary").mock(
        return_value=httpx.Response(200, json=resp_json)
    )

    result = client.get_dev_info_many([])

    req = route.calls[0].request
    # When keys is empty, join produces empty string
    assert req.url.params["issueId"] == ""
    assert result["detail"] == []


# ---------------------------------------------------------------------------
# 4. batch_create_issues
# ---------------------------------------------------------------------------


@respx.mock
def test_batch_create_issues_success(client: JiraClient) -> None:
    resp_json = {
        "issues": [
            {"id": "10001", "key": "PROJ-1", "self": f"{BASE_URL}/rest/api/2/issue/10001"},
            {"id": "10002", "key": "PROJ-2", "self": f"{BASE_URL}/rest/api/2/issue/10002"},
            {"id": "10003", "key": "PROJ-3", "self": f"{BASE_URL}/rest/api/2/issue/10003"},
        ],
        "errors": [],
    }
    route = respx.post(f"{BASE_URL}/rest/api/2/issue/bulk").mock(return_value=httpx.Response(201, json=resp_json))

    issues = [
        {"project": {"key": "PROJ"}, "summary": f"Issue {i}", "issuetype": {"name": "Task"}}
        for i in range(1, 4)
    ]
    result = client.batch_create_issues(issues)

    req = route.calls[0].request
    body = json.loads(req.content)
    assert "issueUpdates" in body
    assert len(body["issueUpdates"]) == 3
    assert body["issueUpdates"][0]["fields"]["summary"] == "Issue 1"
    assert len(result["issues"]) == 3
    assert result["errors"] == []


@respx.mock
def test_batch_create_issues_partial_failure(client: JiraClient) -> None:
    resp_json = {
        "issues": [
            {"id": "10001", "key": "PROJ-1", "self": f"{BASE_URL}/rest/api/2/issue/10001"},
        ],
        "errors": [
            {"status": 400, "elementErrors": {"errorMessages": ["summary is required"], "errors": {}}, "failedElementNumber": 1}
        ],
    }
    respx.post(f"{BASE_URL}/rest/api/2/issue/bulk").mock(return_value=httpx.Response(201, json=resp_json))

    result = client.batch_create_issues([
        {"project": {"key": "PROJ"}, "summary": "Good issue", "issuetype": {"name": "Task"}},
        {"project": {"key": "PROJ"}, "issuetype": {"name": "Task"}},  # missing summary
    ])

    assert len(result["issues"]) == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["failedElementNumber"] == 1


@respx.mock
def test_batch_create_issues_wraps_fields_in_issue_updates(client: JiraClient) -> None:
    """Verify the payload structure: each issue dict is wrapped under issueUpdates[].fields."""
    resp_json = {"issues": [{"id": "1", "key": "PROJ-1"}], "errors": []}
    route = respx.post(f"{BASE_URL}/rest/api/2/issue/bulk").mock(return_value=httpx.Response(201, json=resp_json))

    client.batch_create_issues([{"project": {"key": "PROJ"}, "summary": "Test", "issuetype": {"name": "Bug"}}])

    body = json.loads(route.calls[0].request.content)
    assert body["issueUpdates"][0]["fields"]["summary"] == "Test"
    assert body["issueUpdates"][0]["fields"]["project"]["key"] == "PROJ"


# ---------------------------------------------------------------------------
# 5. Service desk methods
# ---------------------------------------------------------------------------


@respx.mock
def test_list_service_desks_returns_list(client: JiraClient) -> None:
    resp_json = {
        "values": [
            {"id": "1", "projectKey": "SD", "projectName": "Service Desk"},
            {"id": "2", "projectKey": "IT", "projectName": "IT Help"},
        ],
        "isLastPage": True,
    }
    respx.get(f"{BASE_URL}/rest/servicedeskapi/servicedesk").mock(return_value=httpx.Response(200, json=resp_json))

    result = client.list_service_desks()

    assert len(result) == 2
    assert result[0]["projectKey"] == "SD"
    assert result[1]["id"] == "2"


@respx.mock
def test_list_service_desks_plain_list_response(client: JiraClient) -> None:
    """Some endpoints return plain list instead of paginated wrapper."""
    resp_json = [
        {"id": "1", "projectKey": "SD"},
        {"id": "2", "projectKey": "IT"},
    ]
    respx.get(f"{BASE_URL}/rest/servicedeskapi/servicedesk").mock(return_value=httpx.Response(200, json=resp_json))

    result = client.list_service_desks()

    assert len(result) == 2


@respx.mock
def test_get_service_desk_queues(client: JiraClient) -> None:
    resp_json = {
        "values": [
            {"id": "10", "name": "Open Issues", "issueCount": 5},
            {"id": "11", "name": "In Progress", "issueCount": 2},
        ]
    }
    respx.get(f"{BASE_URL}/rest/servicedeskapi/servicedesk/1/queue").mock(return_value=httpx.Response(200, json=resp_json))

    result = client.get_service_desk_queues(1)

    assert len(result) == 2
    assert result[0]["name"] == "Open Issues"
    assert result[1]["issueCount"] == 2


@respx.mock
def test_get_service_desk_queues_empty(client: JiraClient) -> None:
    resp_json: dict[str, Any] = {"values": []}
    respx.get(f"{BASE_URL}/rest/servicedeskapi/servicedesk/5/queue").mock(return_value=httpx.Response(200, json=resp_json))

    result = client.get_service_desk_queues(5)

    assert result == []


@respx.mock
def test_get_queue_issues_returns_list(client: JiraClient) -> None:
    resp_json = {
        "issues": [
            {
                "id": "20001",
                "key": "SD-42",
                "fields": {"summary": "My printer is broken", "status": {"name": "Open"}},
            }
        ],
        "isLastPage": True,
    }
    respx.get(f"{BASE_URL}/rest/servicedeskapi/servicedesk/1/queue/10/issue").mock(
        return_value=httpx.Response(200, json=resp_json)
    )

    result = client.get_queue_issues(1, 10)

    assert len(result) == 1
    assert result[0]["key"] == "SD-42"


@respx.mock
def test_get_queue_issues_uses_values_fallback(client: JiraClient) -> None:
    """When 'issues' key is absent, fallback to 'values'."""
    resp_json = {
        "values": [
            {"id": "20002", "key": "IT-5"},
        ]
    }
    respx.get(f"{BASE_URL}/rest/servicedeskapi/servicedesk/2/queue/11/issue").mock(
        return_value=httpx.Response(200, json=resp_json)
    )

    result = client.get_queue_issues(2, 11)

    assert len(result) == 1
    assert result[0]["key"] == "IT-5"


@respx.mock
def test_get_queue_issues_empty(client: JiraClient) -> None:
    resp_json: dict[str, Any] = {"issues": [], "isLastPage": True}
    respx.get(f"{BASE_URL}/rest/servicedeskapi/servicedesk/1/queue/99/issue").mock(
        return_value=httpx.Response(200, json=resp_json)
    )

    result = client.get_queue_issues(1, 99)

    assert result == []


# ---------------------------------------------------------------------------
# 6. Remote issue links and remove_issue_link
# ---------------------------------------------------------------------------


@respx.mock
def test_create_remote_issue_link_with_relationship(client: JiraClient) -> None:
    resp_json = {
        "id": 300,
        "self": f"{BASE_URL}/rest/api/2/issue/PROJ-10/remotelink/300",
    }
    route = respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-10/remotelink").mock(
        return_value=httpx.Response(201, json=resp_json)
    )

    result = client.create_remote_issue_link(
        "PROJ-10",
        "https://docs.example.com/spec",
        "Technical Spec",
        relationship="Wiki Page",
    )

    body = json.loads(route.calls[0].request.content)
    assert body["object"]["url"] == "https://docs.example.com/spec"
    assert body["object"]["title"] == "Technical Spec"
    assert body["relationship"] == "Wiki Page"
    assert result["id"] == 300


@respx.mock
def test_create_remote_issue_link_without_relationship(client: JiraClient) -> None:
    resp_json = {"id": 301, "self": f"{BASE_URL}/rest/api/2/issue/PROJ-1/remotelink/301"}
    route = respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-1/remotelink").mock(
        return_value=httpx.Response(201, json=resp_json)
    )

    client.create_remote_issue_link("PROJ-1", "https://example.com", "Example")

    body = json.loads(route.calls[0].request.content)
    assert "relationship" not in body
    assert body["object"]["url"] == "https://example.com"


@respx.mock
def test_remove_issue_link_calls_delete(client: JiraClient) -> None:
    route = respx.delete(f"{BASE_URL}/rest/api/2/issueLink/555").mock(
        return_value=httpx.Response(204)
    )

    client.remove_issue_link("555")

    assert route.called
    assert route.call_count == 1


@respx.mock
def test_remove_issue_link_404_raises_not_found(client: JiraClient) -> None:
    respx.delete(f"{BASE_URL}/rest/api/2/issueLink/999").mock(
        return_value=httpx.Response(404, text="Link not found")
    )

    with pytest.raises(NotFoundError):
        client.remove_issue_link("999")


# ---------------------------------------------------------------------------
# 7. get_attachment_content
# ---------------------------------------------------------------------------


@respx.mock
def test_get_attachment_content_returns_all_attachments(client: JiraClient) -> None:
    resp_json = {
        "id": "PROJ-1",
        "key": "PROJ-1",
        "fields": {
            "attachment": [
                {"id": "1", "filename": "spec.pdf", "mimeType": "application/pdf", "size": 12345},
                {"id": "2", "filename": "screenshot.png", "mimeType": "image/png", "size": 4096},
                {"id": "3", "filename": "diagram.jpg", "mimeType": "image/jpeg", "size": 8192},
            ]
        },
    }
    route = respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(return_value=httpx.Response(200, json=resp_json))

    result = client.get_attachment_content("PROJ-1")

    req = route.calls[0].request
    assert req.url.params["fields"] == "attachment"
    assert len(result) == 3
    assert result[0]["filename"] == "spec.pdf"


@respx.mock
def test_get_attachment_content_empty(client: JiraClient) -> None:
    resp_json = {
        "id": "PROJ-2",
        "key": "PROJ-2",
        "fields": {"attachment": []},
    }
    respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-2").mock(return_value=httpx.Response(200, json=resp_json))

    result = client.get_attachment_content("PROJ-2")

    assert result == []


@respx.mock
def test_get_issue_images_filters_image_attachments(client: JiraClient) -> None:
    resp_json = {
        "id": "PROJ-3",
        "key": "PROJ-3",
        "fields": {
            "attachment": [
                {"id": "1", "filename": "notes.txt", "mimeType": "text/plain", "size": 100},
                {"id": "2", "filename": "photo.png", "mimeType": "image/png", "size": 2048},
                {"id": "3", "filename": "chart.jpg", "mimeType": "image/jpeg", "size": 4096},
                {"id": "4", "filename": "data.csv", "mimeType": "text/csv", "size": 512},
            ]
        },
    }
    respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-3").mock(return_value=httpx.Response(200, json=resp_json))

    result = client.get_issue_images("PROJ-3")

    assert len(result) == 2
    assert all(a["mimeType"].startswith("image/") for a in result)
    assert result[0]["filename"] == "photo.png"
    assert result[1]["filename"] == "chart.jpg"


@respx.mock
def test_get_issue_images_no_images(client: JiraClient) -> None:
    resp_json = {
        "id": "PROJ-4",
        "key": "PROJ-4",
        "fields": {
            "attachment": [
                {"id": "1", "filename": "document.pdf", "mimeType": "application/pdf", "size": 99},
            ]
        },
    }
    respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-4").mock(return_value=httpx.Response(200, json=resp_json))

    result = client.get_issue_images("PROJ-4")

    assert result == []


# ---------------------------------------------------------------------------
# 8. Search with special characters / edge cases
# ---------------------------------------------------------------------------


@respx.mock
def test_search_jql_with_quotes_and_special_chars(client: JiraClient) -> None:
    """JQL with quotes and special characters should be sent as-is."""
    jql = 'project = "MY PROJECT" AND summary ~ "fix: crash"'
    route = respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        return_value=httpx.Response(200, json={"total": 0, "startAt": 0, "maxResults": 50, "issues": []})
    )

    result = client.search(jql)

    req = route.calls[0].request
    assert req.url.params["jql"] == jql
    assert result.total == 0
    assert result.issues == []


@respx.mock
def test_search_empty_result_set(client: JiraClient) -> None:
    resp_json: dict[str, Any] = {"total": 0, "startAt": 0, "maxResults": 50, "issues": []}
    respx.get(f"{BASE_URL}/rest/api/2/search").mock(return_value=httpx.Response(200, json=resp_json))

    result = client.search("project = NONEXISTENT AND status = Done")

    assert result.total == 0
    assert result.issues == []


@respx.mock
def test_search_with_start_at(client: JiraClient) -> None:
    resp_json: dict[str, Any] = {"total": 100, "startAt": 50, "maxResults": 10, "issues": []}
    route = respx.get(f"{BASE_URL}/rest/api/2/search").mock(return_value=httpx.Response(200, json=resp_json))

    client.search("project = PROJ", start_at=50, max_results=10)

    req = route.calls[0].request
    assert req.url.params["startAt"] == "50"
    assert req.url.params["maxResults"] == "10"


# ---------------------------------------------------------------------------
# 9. get_issue with fields/expand params
# ---------------------------------------------------------------------------


@respx.mock
def test_get_issue_with_fields_param(client: JiraClient) -> None:
    resp_json = {
        "id": "10001",
        "key": "PROJ-1",
        "fields": {"summary": "Test issue", "status": {"name": "Open"}},
    }
    route = respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(return_value=httpx.Response(200, json=resp_json))

    client.get_issue("PROJ-1", fields=["summary", "status", "assignee"])

    req = route.calls[0].request
    fields_param = req.url.params["fields"]
    assert "summary" in fields_param
    assert "status" in fields_param
    assert "assignee" in fields_param
    # Confirm comma-separated
    assert fields_param == "summary,status,assignee"


@respx.mock
def test_get_issue_with_expand_param(client: JiraClient) -> None:
    resp_json = {
        "id": "10001",
        "key": "PROJ-1",
        "fields": {"summary": "Test issue"},
    }
    route = respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(return_value=httpx.Response(200, json=resp_json))

    client.get_issue("PROJ-1", expand="renderedFields,changelog")

    req = route.calls[0].request
    assert req.url.params["expand"] == "renderedFields,changelog"


@respx.mock
def test_get_issue_no_params_omits_query_string(client: JiraClient) -> None:
    resp_json = {
        "id": "10002",
        "key": "PROJ-2",
        "fields": {"summary": "Another issue"},
    }
    route = respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-2").mock(return_value=httpx.Response(200, json=resp_json))

    client.get_issue("PROJ-2")

    req = route.calls[0].request
    assert "fields" not in req.url.params
    assert "expand" not in req.url.params


@respx.mock
def test_get_issue_with_fields_and_expand(client: JiraClient) -> None:
    resp_json = {
        "id": "10003",
        "key": "PROJ-3",
        "fields": {"summary": "Third issue"},
    }
    route = respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-3").mock(return_value=httpx.Response(200, json=resp_json))

    client.get_issue("PROJ-3", fields=["summary", "description"], expand="changelog")

    req = route.calls[0].request
    assert req.url.params["fields"] == "summary,description"
    assert req.url.params["expand"] == "changelog"


# ---------------------------------------------------------------------------
# 10. upload_attachment
# ---------------------------------------------------------------------------


@respx.mock
def test_upload_attachment_success(client: JiraClient) -> None:
    resp_json = [
        {
            "id": "10100",
            "filename": "report.pdf",
            "size": 20480,
            "mimeType": "application/pdf",
            "self": f"{BASE_URL}/rest/api/2/attachment/10100",
        }
    ]
    respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-5/attachments").mock(
        return_value=httpx.Response(200, json=resp_json)
    )

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(b"%PDF-1.4 test content")
        tmp_path = f.name

    result = client.upload_attachment("PROJ-5", tmp_path)

    assert isinstance(result, list)
    assert result[0]["id"] == "10100"
    assert result[0]["filename"] == "report.pdf"


@respx.mock
def test_upload_attachment_403_raises_forbidden(client: JiraClient) -> None:
    respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-5/attachments").mock(
        return_value=httpx.Response(403, text="Forbidden")
    )

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"data")
        tmp_path = f.name

    with pytest.raises(ForbiddenError):
        client.upload_attachment("PROJ-5", tmp_path)


def test_upload_attachment_missing_file_raises(client: JiraClient) -> None:
    with pytest.raises(FileNotFoundError):
        client.upload_attachment("PROJ-5", "/no/such/file/here.txt")


@respx.mock
def test_upload_attachment_sends_x_atlassian_token_header(client: JiraClient) -> None:
    """X-Atlassian-Token: nocheck header is required for attachment uploads."""
    resp_json = [{"id": "200", "filename": "doc.txt"}]
    route = respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-6/attachments").mock(
        return_value=httpx.Response(200, json=resp_json)
    )

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"hello")
        tmp_path = f.name

    client.upload_attachment("PROJ-6", tmp_path)

    req = route.calls[0].request
    assert req.headers.get("X-Atlassian-Token") == "nocheck"
