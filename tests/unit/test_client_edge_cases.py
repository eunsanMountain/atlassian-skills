from __future__ import annotations

import httpx
import pytest
import respx

from atlassian_skills.core.auth import Credential
from atlassian_skills.core.client import BaseClient, _retry_wait
from atlassian_skills.core.errors import (
    AuthError,
    ConflictError,
    ForbiddenError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)
from atlassian_skills.core.pagination import collect_all, paginate_links, paginate_offset

BASE_URL = "https://jira.example.com"
PAT_CRED = Credential(method="pat", token="test-token")


def make_client(**kwargs: object) -> BaseClient:
    return BaseClient(BASE_URL, PAT_CRED, **kwargs)


# ---------------------------------------------------------------------------
# Retry logic — Retry-After header
# ---------------------------------------------------------------------------


def test_retry_wait_integer_retry_after() -> None:
    """Retry-After: integer seconds is parsed and capped at 30."""
    response = httpx.Response(429, headers={"Retry-After": "5"})
    assert _retry_wait(response, default_delay=1.0) == 5.0


def test_retry_wait_large_retry_after_is_capped() -> None:
    """Retry-After larger than 30 is capped to 30."""
    response = httpx.Response(429, headers={"Retry-After": "999"})
    assert _retry_wait(response, default_delay=1.0) == 30.0


def test_retry_wait_http_date_falls_back_to_default() -> None:
    """HTTP-date Retry-After is not supported; falls back to default_delay."""
    # RFC 7231 date format — not numeric, so float() raises ValueError
    response = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2025 07:28:00 GMT"})
    assert _retry_wait(response, default_delay=2.5) == 2.5


def test_retry_wait_missing_header_uses_default() -> None:
    """No Retry-After header → returns default_delay."""
    response = httpx.Response(429)
    assert _retry_wait(response, default_delay=3.0) == 3.0


@respx.mock
def test_429_triggers_retry(capsys: pytest.CaptureFixture[str]) -> None:
    """429 response must trigger at least one retry attempt."""
    route = respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"key": "PROJ-1"}),
        ]
    )
    client = make_client(max_retries=1)
    resp = client.get("/rest/api/2/issue/PROJ-1")
    assert resp.status_code == 200
    assert route.call_count == 2
    assert "rate-limited" in capsys.readouterr().err


@respx.mock
def test_503_triggers_retry(capsys: pytest.CaptureFixture[str]) -> None:
    """503 response must trigger at least one retry attempt."""
    route = respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(
        side_effect=[
            httpx.Response(503, text="Service Unavailable"),
            httpx.Response(200, json={"key": "PROJ-1"}),
        ]
    )
    client = make_client(max_retries=1)
    resp = client.get("/rest/api/2/issue/PROJ-1")
    assert resp.status_code == 200
    assert route.call_count == 2
    assert "server error 503" in capsys.readouterr().err


@respx.mock
def test_max_retries_exhausted_raises_network_error() -> None:
    """When all retries are exhausted the client raises NetworkError."""
    respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        return_value=httpx.Response(502, text="Bad Gateway")
    )
    client = make_client(max_retries=2)
    with pytest.raises(NetworkError) as exc_info:
        client.get("/rest/api/2/search")
    assert exc_info.value.http_status == 502


@respx.mock
def test_non_retryable_400_no_retry() -> None:
    """400 must NOT be retried — route called exactly once."""
    route = respx.post(f"{BASE_URL}/rest/api/2/issue").mock(
        return_value=httpx.Response(400, json={"errorMessages": ["Bad request"]})
    )
    client = make_client(max_retries=3)
    with pytest.raises(ValidationError):
        client.post("/rest/api/2/issue", json={})
    assert route.call_count == 1


@respx.mock
def test_non_retryable_401_no_retry() -> None:
    """401 must NOT be retried."""
    route = respx.get(f"{BASE_URL}/rest/api/2/myself").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    client = make_client(max_retries=3)
    with pytest.raises(AuthError):
        client.get("/rest/api/2/myself")
    assert route.call_count == 1


@respx.mock
def test_non_retryable_403_no_retry() -> None:
    """403 must NOT be retried."""
    route = respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(
        return_value=httpx.Response(403, text="Forbidden")
    )
    client = make_client(max_retries=3)
    with pytest.raises(ForbiddenError):
        client.get("/rest/api/2/issue/PROJ-1")
    assert route.call_count == 1


@respx.mock
def test_non_retryable_404_no_retry() -> None:
    """404 must NOT be retried."""
    route = respx.get(f"{BASE_URL}/rest/api/2/issue/NOPE-999").mock(
        return_value=httpx.Response(404, text="Not found")
    )
    client = make_client(max_retries=3)
    with pytest.raises(NotFoundError):
        client.get("/rest/api/2/issue/NOPE-999")
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# Error mapping completeness
# ---------------------------------------------------------------------------


@respx.mock
def test_400_raises_validation_error() -> None:
    respx.post(f"{BASE_URL}/rest/api/2/issue").mock(
        return_value=httpx.Response(400, json={"errorMessages": ["Field required"]})
    )
    client = make_client(max_retries=0)
    with pytest.raises(ValidationError) as exc_info:
        client.post("/rest/api/2/issue", json={})
    assert exc_info.value.http_status == 400


@respx.mock
def test_401_raises_auth_error() -> None:
    respx.get(f"{BASE_URL}/rest/api/2/myself").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    client = make_client(max_retries=0)
    with pytest.raises(AuthError) as exc_info:
        client.get("/rest/api/2/myself")
    assert exc_info.value.http_status == 401


@respx.mock
def test_403_raises_forbidden_error() -> None:
    respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(
        return_value=httpx.Response(403, text="Forbidden")
    )
    client = make_client(max_retries=0)
    with pytest.raises(ForbiddenError) as exc_info:
        client.get("/rest/api/2/issue/PROJ-1")
    assert exc_info.value.http_status == 403


@respx.mock
def test_404_raises_not_found_error() -> None:
    respx.get(f"{BASE_URL}/rest/api/2/issue/GONE-1").mock(
        return_value=httpx.Response(404, text="Not found")
    )
    client = make_client(max_retries=0)
    with pytest.raises(NotFoundError) as exc_info:
        client.get("/rest/api/2/issue/GONE-1")
    assert exc_info.value.http_status == 404


@respx.mock
def test_409_raises_conflict_error() -> None:
    respx.put(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(
        return_value=httpx.Response(409, json={"message": "Version mismatch"})
    )
    client = make_client(max_retries=0)
    with pytest.raises(ConflictError) as exc_info:
        client.put("/rest/api/2/issue/PROJ-1", json={"version": 1})
    assert exc_info.value.http_status == 409
    assert exc_info.value.hint is not None


@respx.mock
def test_429_exhausted_raises_rate_limit_error() -> None:
    """When 429 exhausts all retries, RateLimitError is raised."""
    respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"})
    )
    client = make_client(max_retries=1)
    with pytest.raises(RateLimitError) as exc_info:
        client.get("/rest/api/2/search")
    assert exc_info.value.http_status == 429


@respx.mock
def test_500_exhausted_raises_network_error() -> None:
    respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    client = make_client(max_retries=1)
    with pytest.raises(NetworkError) as exc_info:
        client.get("/rest/api/2/search")
    assert exc_info.value.http_status == 500


@respx.mock
def test_502_exhausted_raises_network_error() -> None:
    respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        return_value=httpx.Response(502, text="Bad Gateway")
    )
    client = make_client(max_retries=1)
    with pytest.raises(NetworkError) as exc_info:
        client.get("/rest/api/2/search")
    assert exc_info.value.http_status == 502


@respx.mock
def test_connection_error_raises_network_error() -> None:
    """A low-level connection error maps to NetworkError."""
    respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )
    client = make_client(max_retries=0)
    with pytest.raises(NetworkError) as exc_info:
        client.get("/rest/api/2/issue/PROJ-1")
    assert exc_info.value.http_status is None
    assert "Connection" in exc_info.value.message or "error" in exc_info.value.message.lower()


# ---------------------------------------------------------------------------
# SSL verify parameter
# ---------------------------------------------------------------------------


def test_ssl_verify_true_by_default() -> None:
    """BaseClient stores verify=True (default) and passes it to httpx."""
    # We patch httpx.Client to capture the kwargs it receives
    from unittest.mock import MagicMock, patch

    with patch("atlassian_skills.core.client.httpx.Client") as mock_cls:
        mock_cls.return_value = MagicMock()
        BaseClient(BASE_URL, PAT_CRED)
        _, kwargs = mock_cls.call_args
        assert kwargs["verify"] is True


def test_ssl_verify_false() -> None:
    """BaseClient passes verify=False to the underlying httpx.Client."""
    from unittest.mock import MagicMock, patch

    with patch("atlassian_skills.core.client.httpx.Client") as mock_cls:
        mock_cls.return_value = MagicMock()
        BaseClient(BASE_URL, PAT_CRED, verify=False)
        _, kwargs = mock_cls.call_args
        assert kwargs["verify"] is False


def test_ssl_verify_custom_ca_bundle(tmp_path: pytest.TempPathFactory) -> None:
    """BaseClient passes a CA bundle path string to the underlying httpx.Client."""
    from unittest.mock import MagicMock, patch

    ca_path = "/path/to/ca-bundle.crt"
    with patch("atlassian_skills.core.client.httpx.Client") as mock_cls:
        mock_cls.return_value = MagicMock()
        BaseClient(BASE_URL, PAT_CRED, verify=ca_path)
        _, kwargs = mock_cls.call_args
        assert kwargs["verify"] == ca_path


# ---------------------------------------------------------------------------
# Pagination helpers (unit-level, no HTTP)
# ---------------------------------------------------------------------------


def test_paginate_offset_multiple_pages() -> None:
    """paginate_offset yields pages until start_at >= total."""
    calls: list[tuple[int, int]] = []

    def fetch(start_at: int, max_results: int) -> dict:
        calls.append((start_at, max_results))
        if start_at == 0:
            return {"startAt": 0, "maxResults": 2, "total": 5, "items": [{"id": 1}, {"id": 2}]}
        if start_at == 2:
            return {"startAt": 2, "maxResults": 2, "total": 5, "items": [{"id": 3}, {"id": 4}]}
        return {"startAt": 4, "maxResults": 2, "total": 5, "items": [{"id": 5}]}

    pages = list(paginate_offset(fetch, max_results_per_page=2, items_key="items"))
    assert len(pages) == 3
    assert calls == [(0, 2), (2, 2), (4, 2)]


def test_paginate_offset_single_page() -> None:
    """paginate_offset stops after one page when total <= page_size."""
    def fetch(start_at: int, max_results: int) -> dict:
        return {"startAt": 0, "maxResults": 50, "total": 3, "items": [1, 2, 3]}

    pages = list(paginate_offset(fetch, max_results_per_page=50, items_key="items"))
    assert len(pages) == 1


def test_paginate_offset_limit_respected() -> None:
    """paginate_offset stops after collecting limit items."""
    calls: list[tuple[int, int]] = []

    def fetch(start_at: int, max_results: int) -> dict:
        calls.append((start_at, max_results))
        return {"startAt": start_at, "maxResults": max_results, "total": 100, "items": list(range(max_results))}

    pages = list(paginate_offset(fetch, max_results_per_page=5, limit=5, items_key="items"))
    assert len(pages) == 1
    assert len(calls) == 1


def test_paginate_links_multiple_pages() -> None:
    """paginate_links follows _links.next until absent."""
    urls_seen: list[str | None] = []

    def fetch(next_url: str | None) -> dict:
        urls_seen.append(next_url)
        if next_url is None:
            return {"results": [{"id": "a"}, {"id": "b"}], "_links": {"next": "/page2"}}
        if next_url == "/page2":
            return {"results": [{"id": "c"}], "_links": {}}

        return {"results": [], "_links": {}}

    pages = list(paginate_links(fetch))
    assert len(pages) == 2
    assert urls_seen == [None, "/page2"]


def test_paginate_links_no_next_link() -> None:
    """paginate_links stops immediately when no _links.next."""
    call_count = 0

    def fetch(next_url: str | None) -> dict:
        nonlocal call_count
        call_count += 1
        return {"results": [{"id": "x"}], "_links": {}}

    pages = list(paginate_links(fetch))
    assert len(pages) == 1
    assert call_count == 1


def test_paginate_links_limit_respected() -> None:
    """paginate_links stops when collected results >= limit."""
    def fetch(next_url: str | None) -> dict:
        return {"results": [{"id": "a"}, {"id": "b"}], "_links": {"next": "/more"}}

    pages = list(paginate_links(fetch, limit=2))
    assert len(pages) == 1  # stops after first page which already has 2 results


def test_collect_all_flattens_pages() -> None:
    """collect_all accumulates items across pages."""
    pages_iter = iter([
        {"items": [1, 2]},
        {"items": [3]},
        {"items": [4, 5]},
    ])
    result = collect_all(pages_iter, items_key="items")
    assert result == [1, 2, 3, 4, 5]


def test_collect_all_missing_key_yields_empty() -> None:
    """collect_all skips pages where items_key is absent."""
    pages_iter = iter([{"other": [1]}, {"items": [2, 3]}])
    result = collect_all(pages_iter, items_key="items")
    assert result == [2, 3]


# ---------------------------------------------------------------------------
# Request method edge cases
# ---------------------------------------------------------------------------


@respx.mock
def test_post_with_no_body() -> None:
    """POST with no json/data body is sent successfully."""
    respx.post(f"{BASE_URL}/rest/api/2/issue/PROJ-1/transitions").mock(
        return_value=httpx.Response(204)
    )
    client = make_client()
    resp = client.post("/rest/api/2/issue/PROJ-1/transitions")
    assert resp.status_code == 204


@respx.mock
def test_get_with_none_params_excluded() -> None:
    """None values in params dict are forwarded; httpx drops them automatically."""
    route = respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        return_value=httpx.Response(200, json={})
    )
    client = make_client()
    # Passing None values — verify no exception is raised
    resp = client.get("/rest/api/2/search", params={"jql": "project=X", "expand": None})
    assert resp.status_code == 200
    assert route.called


@respx.mock
def test_request_honours_custom_timeout() -> None:
    """BaseClient is initialized with the given timeout value."""
    client = make_client(timeout=10.0)
    assert client.timeout == 10.0
    assert client._client.timeout.read == 10.0


@respx.mock
def test_context_manager_closes_client() -> None:
    """__exit__ closes the underlying httpx.Client without error."""
    respx.get(f"{BASE_URL}/rest/api/2/serverInfo").mock(
        return_value=httpx.Response(200, json={"version": "9.0"})
    )
    client = BaseClient(BASE_URL, PAT_CRED)
    with client as c:
        resp = c.get("/rest/api/2/serverInfo")
        assert resp.status_code == 200
    # After __exit__ the client is closed; is_closed is True
    assert client._client.is_closed


# ---------------------------------------------------------------------------
# Auth header edge cases
# ---------------------------------------------------------------------------


@respx.mock
def test_pat_produces_bearer_header() -> None:
    """PAT credential produces Authorization: Bearer <token>."""
    route = respx.get(f"{BASE_URL}/rest/api/2/myself").mock(
        return_value=httpx.Response(200, json={})
    )
    client = make_client()
    client.get("/rest/api/2/myself")
    auth = route.calls[0].request.headers["authorization"]
    assert auth == "Bearer test-token"


@respx.mock
def test_basic_auth_produces_correct_header() -> None:
    """Basic credential produces properly base64-encoded Authorization header."""
    import base64

    username, password = "alice", "s3cr3t"
    expected = base64.b64encode(f"{username}:{password}".encode()).decode()

    cred = Credential(method="basic", token=password, username=username)
    route = respx.get(f"{BASE_URL}/rest/api/2/myself").mock(
        return_value=httpx.Response(200, json={})
    )
    client = BaseClient(BASE_URL, cred)
    client.get("/rest/api/2/myself")
    auth = route.calls[0].request.headers["authorization"]
    assert auth == f"Basic {expected}"


@respx.mock
def test_token_not_in_error_message() -> None:
    """The PAT token must not appear in the error message raised on 401."""
    secret_token = "super-secret-token-xyz"
    cred = Credential(method="pat", token=secret_token)

    respx.get(f"{BASE_URL}/rest/api/2/myself").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    client = BaseClient(BASE_URL, cred)
    with pytest.raises(AuthError) as exc_info:
        client.get("/rest/api/2/myself")
    assert secret_token not in exc_info.value.message
    assert secret_token not in str(exc_info.value)
