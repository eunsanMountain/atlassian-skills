from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx

from atlassian_skills.bitbucket.client import BitbucketClient
from atlassian_skills.bitbucket.models import PullRequest, PullRequestComment, PullRequestParticipant
from atlassian_skills.core.auth import Credential

FIXTURES = Path(__file__).parent.parent / "fixtures" / "bitbucket"
BASE_URL = "https://bitbucket.example.com"
API = "/rest/api/1.0"

cred = Credential(method="pat", token="test-token")
client = BitbucketClient(BASE_URL, cred)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# _get_current_user_slug
# ---------------------------------------------------------------------------


@respx.mock
def test_get_current_user_slug() -> None:
    respx.get(f"{BASE_URL}{API}/users").mock(
        return_value=httpx.Response(200, json={"values": []}, headers={"X-AUSERNAME": "jsmith"})
    )

    # Reset cache
    client._current_user_slug = None
    slug = client._get_current_user_slug()

    assert slug == "jsmith"
    # Second call should use cache (no additional HTTP call)
    assert client._current_user_slug == "jsmith"


# ---------------------------------------------------------------------------
# create_pull_request
# ---------------------------------------------------------------------------


@respx.mock
def test_create_pull_request() -> None:
    fixture = _load("pull-request-create-expected.json")
    route = respx.post(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests").mock(
        return_value=httpx.Response(201, json=fixture)
    )

    result = client.create_pull_request(
        "PROJ",
        "my-repo",
        title="New feature PR",
        from_ref="feature/new",
        to_ref="main",
        description="Description of the PR",
        reviewers=["alice"],
    )

    assert isinstance(result, PullRequest)
    assert result.id == 3
    assert result.title == "New feature PR"
    sent = json.loads(route.calls[0].request.content)
    assert sent["fromRef"]["id"] == "refs/heads/feature/new"
    assert sent["toRef"]["id"] == "refs/heads/main"
    assert sent["reviewers"] == [{"user": {"name": "alice"}}]


# ---------------------------------------------------------------------------
# merge_pull_request (auto-version fetch)
# ---------------------------------------------------------------------------


@respx.mock
def test_merge_pull_request_auto_version() -> None:
    pr_fixture = _load("pull-request-get.json")
    merge_result = dict(pr_fixture, state="MERGED")

    # First call: GET to fetch version
    respx.get(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1").mock(
        return_value=httpx.Response(200, json=pr_fixture)
    )
    # Second call: POST to merge
    route = respx.post(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1/merge").mock(
        return_value=httpx.Response(200, json=merge_result)
    )

    result = client.merge_pull_request("PROJ", "my-repo", 1)

    assert result.state == "MERGED"
    sent = json.loads(route.calls[0].request.content)
    assert sent["version"] == 3  # auto-fetched from PR fixture


# ---------------------------------------------------------------------------
# decline_pull_request
# ---------------------------------------------------------------------------


@respx.mock
def test_decline_pull_request() -> None:
    pr_fixture = _load("pull-request-get.json")
    decline_result = dict(pr_fixture, state="DECLINED")

    respx.get(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1").mock(
        return_value=httpx.Response(200, json=pr_fixture)
    )
    respx.post(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1/decline").mock(
        return_value=httpx.Response(200, json=decline_result)
    )

    result = client.decline_pull_request("PROJ", "my-repo", 1)

    assert result.state == "DECLINED"


# ---------------------------------------------------------------------------
# approve / unapprove
# ---------------------------------------------------------------------------


@respx.mock
def test_approve_pull_request() -> None:
    expected = {
        "user": {"name": "jsmith", "displayName": "John Smith"},
        "role": "REVIEWER",
        "approved": True,
        "status": "APPROVED",
    }
    respx.post(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1/approve").mock(
        return_value=httpx.Response(200, json=expected)
    )

    result = client.approve_pull_request("PROJ", "my-repo", 1)

    assert isinstance(result, PullRequestParticipant)
    assert result.approved is True
    assert result.status == "APPROVED"


@respx.mock
def test_unapprove_pull_request() -> None:
    respx.delete(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1/approve").mock(
        return_value=httpx.Response(204)
    )

    # Should not raise
    client.unapprove_pull_request("PROJ", "my-repo", 1)


# ---------------------------------------------------------------------------
# needs_work
# ---------------------------------------------------------------------------


@respx.mock
def test_needs_work_pull_request() -> None:
    client._current_user_slug = None
    respx.get(f"{BASE_URL}{API}/users").mock(
        return_value=httpx.Response(200, json={"values": []}, headers={"X-AUSERNAME": "jsmith"})
    )
    respx.put(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1/participants/jsmith").mock(
        return_value=httpx.Response(200, json={"status": "NEEDS_WORK"})
    )

    client.needs_work_pull_request("PROJ", "my-repo", 1)
    # No exception = success


# ---------------------------------------------------------------------------
# reopen_pull_request
# ---------------------------------------------------------------------------


@respx.mock
def test_reopen_pull_request() -> None:
    pr_fixture = _load("pull-request-get.json")
    reopen_result = dict(pr_fixture, state="OPEN")

    respx.get(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1").mock(
        return_value=httpx.Response(200, json=pr_fixture)
    )
    respx.post(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1/reopen").mock(
        return_value=httpx.Response(200, json=reopen_result)
    )

    result = client.reopen_pull_request("PROJ", "my-repo", 1)

    assert result.state == "OPEN"


# ---------------------------------------------------------------------------
# add_pull_request_comment
# ---------------------------------------------------------------------------


@respx.mock
def test_add_pull_request_comment() -> None:
    fixture = _load("comment-add-expected.json")
    route = respx.post(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1/comments").mock(
        return_value=httpx.Response(201, json=fixture)
    )

    result = client.add_pull_request_comment("PROJ", "my-repo", 1, text="Great work on this PR!")

    assert isinstance(result, PullRequestComment)
    assert result.id == 200
    sent = json.loads(route.calls[0].request.content)
    assert sent["text"] == "Great work on this PR!"


@respx.mock
def test_add_pull_request_comment_inline() -> None:
    fixture = _load("comment-add-expected.json")
    route = respx.post(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1/comments").mock(
        return_value=httpx.Response(201, json=fixture)
    )

    anchor = {"path": "src/main.py", "line": 42, "lineType": "ADDED"}
    result = client.add_pull_request_comment("PROJ", "my-repo", 1, text="Fix this", anchor=anchor)

    assert isinstance(result, PullRequestComment)
    sent = json.loads(route.calls[0].request.content)
    assert sent["anchor"]["path"] == "src/main.py"
    assert sent["anchor"]["line"] == 42


# ---------------------------------------------------------------------------
# reply_to_comment
# ---------------------------------------------------------------------------


@respx.mock
def test_reply_to_comment() -> None:
    fixture = _load("comment-add-expected.json")
    route = respx.post(f"{BASE_URL}{API}/projects/PROJ/repos/my-repo/pull-requests/1/comments").mock(
        return_value=httpx.Response(201, json=fixture)
    )

    result = client.reply_to_comment("PROJ", "my-repo", 1, 100, text="Thanks for the feedback!")

    assert isinstance(result, PullRequestComment)
    sent = json.loads(route.calls[0].request.content)
    assert sent["parent"]["id"] == 100
    assert sent["text"] == "Thanks for the feedback!"
