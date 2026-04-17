from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx

from atlassian_skills.bitbucket.client import BitbucketClient
from atlassian_skills.bitbucket.models import BuildStatus, DiffStat, PullRequest, PullRequestComment, Task
from atlassian_skills.core.auth import Credential

FIXTURES = Path(__file__).parent.parent / "fixtures" / "bitbucket"
BASE_URL = "https://bitbucket.example.com"
API = "/rest/api/1.0"

cred = Credential(method="pat", token="test-token")
client = BitbucketClient(BASE_URL, cred)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# update_comment (with auto-version)
# ---------------------------------------------------------------------------


@respx.mock
def test_update_comment_auto_version() -> None:
    current = {"id": 100, "text": "old text", "version": 2, "author": {"name": "a", "displayName": "A"}}
    updated = {"id": 100, "text": "new text", "version": 3, "author": {"name": "a", "displayName": "A"}}

    respx.get(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1/comments/100").mock(
        return_value=httpx.Response(200, json=current)
    )
    route = respx.put(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1/comments/100").mock(
        return_value=httpx.Response(200, json=updated)
    )

    result = client.update_comment("PROJ", "my-repo", 1, 100, text="new text")

    assert isinstance(result, PullRequestComment)
    assert result.text == "new text"
    sent = json.loads(route.calls[0].request.content)
    assert sent["version"] == 2
    assert sent["text"] == "new text"


# ---------------------------------------------------------------------------
# delete_comment (version as query param)
# ---------------------------------------------------------------------------


@respx.mock
def test_delete_comment_version_param() -> None:
    current = {"id": 100, "text": "text", "version": 2}
    respx.get(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1/comments/100").mock(
        return_value=httpx.Response(200, json=current)
    )
    route = respx.delete(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1/comments/100").mock(
        return_value=httpx.Response(204)
    )

    client.delete_comment("PROJ", "my-repo", 1, 100)

    params = dict(route.calls[0].request.url.params)
    assert params["version"] == "2"


# ---------------------------------------------------------------------------
# resolve_comment (full body + state)
# ---------------------------------------------------------------------------


@respx.mock
def test_resolve_comment_sends_full_body() -> None:
    current = {"id": 100, "text": "Please fix this", "version": 1}
    resolved = {"id": 100, "text": "Please fix this", "version": 2, "state": "RESOLVED"}

    respx.get(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1/comments/100").mock(
        return_value=httpx.Response(200, json=current)
    )
    route = respx.put(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1/comments/100").mock(
        return_value=httpx.Response(200, json=resolved)
    )

    result = client.resolve_comment("PROJ", "my-repo", 1, 100)

    assert result.state == "RESOLVED"
    sent = json.loads(route.calls[0].request.content)
    assert sent["text"] == "Please fix this"
    assert sent["state"] == "RESOLVED"
    assert sent["version"] == 1


# ---------------------------------------------------------------------------
# reopen_comment
# ---------------------------------------------------------------------------


@respx.mock
def test_reopen_comment() -> None:
    current = {"id": 100, "text": "Fixed now", "version": 2, "state": "RESOLVED"}
    reopened = {"id": 100, "text": "Fixed now", "version": 3, "state": "OPEN"}

    respx.get(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1/comments/100").mock(
        return_value=httpx.Response(200, json=current)
    )
    respx.put(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1/comments/100").mock(
        return_value=httpx.Response(200, json=reopened)
    )

    result = client.reopen_comment("PROJ", "my-repo", 1, 100)

    assert result.state == "OPEN"


# ---------------------------------------------------------------------------
# get_pull_request_diffstat (/changes endpoint)
# ---------------------------------------------------------------------------


@respx.mock
def test_get_pull_request_diffstat_uses_changes() -> None:
    fixture = _load("diffstat-changes.json")
    route = respx.get(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1/changes").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    result = client.get_pull_request_diffstat("PROJ", "my-repo", 1)

    assert len(result) == 2
    assert isinstance(result[0], DiffStat)
    assert result[0].path.to_string == "src/main.py"
    assert result[0].type == "MODIFY"
    assert result[1].type == "ADD"
    assert route.called


# ---------------------------------------------------------------------------
# get_build_statuses (/rest/build-status/1.0/ — different API base)
# ---------------------------------------------------------------------------


@respx.mock
def test_get_build_statuses_uses_build_status_api() -> None:
    fixture = _load("build-status-list.json")
    route = respx.get(f"{BASE_URL}/rest/build-status/1.0/commits/abc123def456").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    result = client.get_build_statuses("abc123def456")

    assert len(result) == 2
    assert isinstance(result[0], BuildStatus)
    assert result[0].state == "SUCCESSFUL"
    assert result[0].key == "ci-build"
    assert result[1].state == "FAILED"
    # Verify it uses the build-status API, not self.API
    assert "/rest/build-status/1.0/" in str(route.calls[0].request.url)


# ---------------------------------------------------------------------------
# list_pull_requests_for_reviewer (inbox with fallback)
# ---------------------------------------------------------------------------


@respx.mock
def test_list_pull_requests_for_reviewer() -> None:
    fixture = _load("pull-request-list.json")
    respx.get(f"{BASE_URL}{API}/inbox/pull-requests").mock(return_value=httpx.Response(200, json=fixture))

    result = client.list_pull_requests_for_reviewer()

    assert len(result) == 2
    assert isinstance(result[0], PullRequest)


@respx.mock
def test_list_pull_requests_for_reviewer_fallback() -> None:
    fixture = _load("pull-request-list.json")
    respx.get(f"{BASE_URL}{API}/inbox/pull-requests").mock(
        return_value=httpx.Response(404, json={"message": "Not found"})
    )
    respx.get(f"{BASE_URL}{API}/dashboard/pull-requests").mock(return_value=httpx.Response(200, json=fixture))

    result = client.list_pull_requests_for_reviewer()

    assert len(result) == 2


# ---------------------------------------------------------------------------
# Task CRUD (top-level /tasks endpoint)
# ---------------------------------------------------------------------------


@respx.mock
def test_list_tasks() -> None:
    fixture = _load("task-list.json")
    respx.get(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1/tasks").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    result = client.list_tasks("PROJ", "my-repo", 1)

    assert len(result) == 2
    assert isinstance(result[0], Task)
    assert result[0].id == 10
    assert result[0].state == "OPEN"
    assert result[1].state == "RESOLVED"


@respx.mock
def test_create_task_uses_top_level_endpoint() -> None:
    fixture = _load("task-create-expected.json")
    route = respx.post(f"{BASE_URL}{API}/tasks").mock(return_value=httpx.Response(201, json=fixture))

    result = client.create_task(text="Update documentation", comment_id=100)

    assert isinstance(result, Task)
    assert result.id == 12
    sent = json.loads(route.calls[0].request.content)
    assert sent["anchor"]["id"] == 100
    assert sent["anchor"]["type"] == "COMMENT"
    assert sent["text"] == "Update documentation"


@respx.mock
def test_update_task() -> None:
    updated = {"id": 10, "text": "Fix naming convention", "state": "RESOLVED"}
    respx.put(f"{BASE_URL}{API}/tasks/10").mock(return_value=httpx.Response(200, json=updated))

    result = client.update_task(10, state="RESOLVED")

    assert result.state == "RESOLVED"


@respx.mock
def test_delete_task() -> None:
    respx.delete(f"{BASE_URL}{API}/tasks/10").mock(return_value=httpx.Response(204))

    client.delete_task(10)
    # No exception = success
