from __future__ import annotations

import httpx
import pytest
import respx

from atlassian_skills.core.auth import Credential
from atlassian_skills.core.client import BaseClient
from atlassian_skills.core.errors import (
    AuthError,
    ConflictError,
    ForbiddenError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)

BASE_URL = "https://jira.example.com"
PAT_CRED = Credential(method="pat", token="test-token")


def make_client(**kwargs: object) -> BaseClient:
    return BaseClient(BASE_URL, PAT_CRED, **kwargs)


# ---------------------------------------------------------------------------
# Basic HTTP methods
# ---------------------------------------------------------------------------


@respx.mock
def test_get_success() -> None:
    respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(
        return_value=httpx.Response(200, json={"id": "1", "key": "PROJ-1"})
    )
    client = make_client()
    resp = client.get("/rest/api/2/issue/PROJ-1")
    assert resp.status_code == 200
    assert resp.json()["key"] == "PROJ-1"


@respx.mock
def test_post_success() -> None:
    respx.post(f"{BASE_URL}/rest/api/2/issue").mock(
        return_value=httpx.Response(201, json={"id": "2", "key": "PROJ-2"})
    )
    client = make_client()
    resp = client.post("/rest/api/2/issue", json={"fields": {}})
    assert resp.status_code == 201


@respx.mock
def test_put_success() -> None:
    respx.put(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(
        return_value=httpx.Response(204)
    )
    client = make_client()
    resp = client.put("/rest/api/2/issue/PROJ-1", json={"fields": {}})
    assert resp.status_code == 204


@respx.mock
def test_delete_success() -> None:
    respx.delete(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(
        return_value=httpx.Response(204)
    )
    client = make_client()
    resp = client.delete("/rest/api/2/issue/PROJ-1")
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Auth header injection
# ---------------------------------------------------------------------------


@respx.mock
def test_pat_auth_header_injected() -> None:
    route = respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(
        return_value=httpx.Response(200, json={})
    )
    client = make_client()
    client.get("/rest/api/2/issue/PROJ-1")
    assert route.called
    sent_request = route.calls[0].request
    assert sent_request.headers["authorization"] == "Bearer test-token"


@respx.mock
def test_basic_auth_header_injected() -> None:
    import base64

    cred = Credential(method="basic", token="secret", username="user")
    expected = base64.b64encode(b"user:secret").decode()
    route = respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(
        return_value=httpx.Response(200, json={})
    )
    client = BaseClient(BASE_URL, cred)
    client.get("/rest/api/2/issue/PROJ-1")
    sent = route.calls[0].request.headers["authorization"]
    assert sent == f"Basic {expected}"


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@respx.mock
def test_404_raises_not_found() -> None:
    respx.get(f"{BASE_URL}/rest/api/2/issue/MISSING").mock(
        return_value=httpx.Response(404, text="Issue not found")
    )
    client = make_client()
    with pytest.raises(NotFoundError) as exc_info:
        client.get("/rest/api/2/issue/MISSING")
    assert exc_info.value.http_status == 404


@respx.mock
def test_401_raises_auth_error() -> None:
    respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    client = make_client()
    with pytest.raises(AuthError):
        client.get("/rest/api/2/issue/PROJ-1")


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------


@respx.mock
def test_429_retries_then_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    route = respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"issues": []}),
        ]
    )
    client = make_client(max_retries=3)
    resp = client.get("/rest/api/2/search")
    assert resp.status_code == 200
    assert route.call_count == 2
    stderr = capsys.readouterr().err
    assert "retry 1/3" in stderr
    assert "rate-limited" in stderr


@respx.mock
def test_5xx_exhausts_retries_raises_network(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        return_value=httpx.Response(503, text="Service Unavailable")
    )
    client = make_client(max_retries=2)
    with pytest.raises(NetworkError) as exc_info:
        client.get("/rest/api/2/search")
    assert exc_info.value.http_status == 503
    stderr = capsys.readouterr().err
    assert "retry 1/2" in stderr
    assert "retry 2/2" in stderr


@respx.mock
def test_500_retries_then_succeeds() -> None:
    route = respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        side_effect=[
            httpx.Response(500, text="Internal Server Error"),
            httpx.Response(200, json={"issues": []}),
        ]
    )
    client = make_client(max_retries=3)
    resp = client.get("/rest/api/2/search")
    assert resp.status_code == 200
    assert route.call_count == 2


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


@respx.mock
def test_timeout_raises_network_error() -> None:
    respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    client = make_client(timeout=1.0, max_retries=0)
    with pytest.raises(NetworkError) as exc_info:
        client.get("/rest/api/2/issue/PROJ-1")
    assert "timed out" in exc_info.value.message.lower()


# ---------------------------------------------------------------------------
# Offset pagination
# ---------------------------------------------------------------------------


@respx.mock
def test_get_paginated_offset_two_pages() -> None:
    page1 = {"startAt": 0, "maxResults": 2, "total": 3, "issues": [{"id": "1"}, {"id": "2"}]}
    page2 = {"startAt": 2, "maxResults": 2, "total": 3, "issues": [{"id": "3"}]}

    route = respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        side_effect=[
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
        ]
    )

    client = make_client()
    results = client.get_paginated_offset(
        "/rest/api/2/search",
        params={"jql": "project=PROJ"},
        items_key="issues",
        max_results_per_page=2,
    )
    assert len(results) == 3
    assert [r["id"] for r in results] == ["1", "2", "3"]
    assert route.call_count == 2


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


@respx.mock
def test_context_manager() -> None:
    respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(
        return_value=httpx.Response(200, json={"key": "PROJ-1"})
    )
    with BaseClient(BASE_URL, PAT_CRED) as client:
        resp = client.get("/rest/api/2/issue/PROJ-1")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Success paths (extended)
# ---------------------------------------------------------------------------


@respx.mock
def test_get_success_returns_json_body() -> None:
    respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-10").mock(
        return_value=httpx.Response(200, json={"id": "10", "key": "PROJ-10", "summary": "Hello"})
    )
    client = make_client()
    resp = client.get("/rest/api/2/issue/PROJ-10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "PROJ-10"
    assert body["summary"] == "Hello"


@respx.mock
def test_post_with_json_body() -> None:
    route = respx.post(f"{BASE_URL}/rest/api/2/issue").mock(
        return_value=httpx.Response(201, json={"id": "99", "key": "PROJ-99"})
    )
    client = make_client()
    resp = client.post("/rest/api/2/issue", json={"fields": {"summary": "New issue"}})
    assert resp.status_code == 201
    assert resp.json()["key"] == "PROJ-99"
    assert route.called
    sent = route.calls[0].request
    assert b"New issue" in sent.content


@respx.mock
def test_put_with_json_body() -> None:
    route = respx.put(f"{BASE_URL}/rest/api/2/issue/PROJ-5").mock(
        return_value=httpx.Response(200, json={"key": "PROJ-5"})
    )
    client = make_client()
    resp = client.put("/rest/api/2/issue/PROJ-5", json={"fields": {"summary": "Updated"}})
    assert resp.status_code == 200
    sent = route.calls[0].request
    assert b"Updated" in sent.content


@respx.mock
def test_delete_success_204() -> None:
    respx.delete(f"{BASE_URL}/rest/api/2/issue/PROJ-7").mock(
        return_value=httpx.Response(204)
    )
    client = make_client()
    resp = client.delete("/rest/api/2/issue/PROJ-7")
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Retry scenarios — parametrized
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
@respx.mock
def test_retry_then_success(status: int, capsys: pytest.CaptureFixture[str]) -> None:
    """First response is retryable, second is 200 — should succeed."""
    route = respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        side_effect=[
            httpx.Response(status, headers={"Retry-After": "0"} if status == 429 else {}),
            httpx.Response(200, json={"issues": []}),
        ]
    )
    client = make_client(max_retries=3)
    resp = client.get("/rest/api/2/search")
    assert resp.status_code == 200
    assert route.call_count == 2
    stderr = capsys.readouterr().err
    assert "retry 1/3" in stderr


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
@respx.mock
def test_retry_max_exceeded_raises(status: int) -> None:
    """All retries return retryable status — must raise an error after exhausting retries."""
    respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        return_value=httpx.Response(status, headers={"Retry-After": "0"} if status == 429 else {})
    )
    client = make_client(max_retries=2)
    with pytest.raises((NetworkError, RateLimitError)) as exc_info:
        client.get("/rest/api/2/search")
    assert exc_info.value.http_status == status


@respx.mock
def test_retry_after_header_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry-After: 2 should result in a ~2s wait (mocked via time.sleep)."""
    import atlassian_skills.core.client as client_mod

    sleeps: list[float] = []
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: sleeps.append(s))

    respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json={}),
        ]
    )
    client = make_client(max_retries=3)
    resp = client.get("/rest/api/2/search")
    assert resp.status_code == 200
    assert len(sleeps) == 1
    assert sleeps[0] == 2.0


@respx.mock
def test_retry_after_capped_at_30(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry-After: 999 should be capped to 30s."""
    import atlassian_skills.core.client as client_mod

    sleeps: list[float] = []
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: sleeps.append(s))

    respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "999"}),
            httpx.Response(200, json={}),
        ]
    )
    client = make_client(max_retries=3)
    resp = client.get("/rest/api/2/search")
    assert resp.status_code == 200
    assert len(sleeps) == 1
    assert sleeps[0] == 30.0


# ---------------------------------------------------------------------------
# Error mapping — parametrized (non-retryable)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,error_cls",
    [
        (400, ValidationError),
        (401, AuthError),
        (403, ForbiddenError),
        (404, NotFoundError),
        (409, ConflictError),
    ],
)
@respx.mock
def test_non_retryable_raises_correct_error(status: int, error_cls: type) -> None:
    """Non-retryable HTTP errors map to the correct AtlasError subclass."""
    respx.get(f"{BASE_URL}/rest/api/2/resource").mock(
        return_value=httpx.Response(status, text="error body")
    )
    client = make_client(max_retries=3)
    with pytest.raises(error_cls) as exc_info:
        client.get("/rest/api/2/resource")
    assert exc_info.value.http_status == status


# ---------------------------------------------------------------------------
# Network errors
# ---------------------------------------------------------------------------


@respx.mock
def test_timeout_raises_network_error_message() -> None:
    """TimeoutException wraps into NetworkError with meaningful message."""
    respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(
        side_effect=httpx.TimeoutException("read timeout")
    )
    client = make_client(max_retries=0)
    with pytest.raises(NetworkError) as exc_info:
        client.get("/rest/api/2/issue/PROJ-1")
    assert exc_info.value.http_status is None
    assert "timed out" in exc_info.value.message.lower()


@respx.mock
def test_connection_error_raises_network_error() -> None:
    """ConnectError wraps into NetworkError."""
    respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )
    client = make_client(max_retries=0)
    with pytest.raises(NetworkError) as exc_info:
        client.get("/rest/api/2/issue/PROJ-1")
    assert exc_info.value.http_status is None


# ---------------------------------------------------------------------------
# Context manager (extended)
# ---------------------------------------------------------------------------


def test_context_manager_closes_client() -> None:
    """__exit__ closes the underlying httpx.Client (is_closed becomes True)."""
    client = BaseClient(BASE_URL, PAT_CRED)
    assert not client._client.is_closed
    with client:
        pass
    assert client._client.is_closed
