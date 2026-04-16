from __future__ import annotations

# Regression tests for 7 bug fixes in atlassian-skills.
# Each test is written to FAIL on the old (broken) code and PASS on the
# fixed code.  Where HTTP is involved, respx mocks all network calls.
import json
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx
from typer.testing import CliRunner

from atlassian_skills.cli.main import app
from atlassian_skills.core.auth import Credential
from atlassian_skills.core.client import BaseClient
from atlassian_skills.core.config import Config
from atlassian_skills.core.errors import (
    NetworkError,
    RateLimitError,
    ValidationError,
)
from atlassian_skills.core.pagination import paginate_offset
from atlassian_skills.core.stdin import read_body

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

BASE_URL = "https://jira.example.com"
CONFLUENCE_URL = "https://confluence.example.com"
PAT_CRED = Credential(method="pat", token="test-token")

_runner = CliRunner()

_JIRA_ENV = {
    "ATLS_DEFAULT_JIRA_URL": BASE_URL,
    "ATLS_DEFAULT_JIRA_TOKEN": "test-token",
}
_CONFLUENCE_ENV = {
    "ATLS_DEFAULT_CONFLUENCE_URL": CONFLUENCE_URL,
    "ATLS_DEFAULT_CONFLUENCE_TOKEN": "test-token",
}


def make_client(**kwargs: object) -> BaseClient:
    return BaseClient(BASE_URL, PAT_CRED, **kwargs)


# ===========================================================================
# Bug #1 — cli/confluence.py: body_repr=md uses body_storage key correctly
# ===========================================================================


@respx.mock
def test_confluence_page_get_body_repr_md_converts_content() -> None:
    """--body-repr=md must convert body_storage via confluence_storage_to_md.

    Old code read data.get("body", {}).get("storage", {}).get("value", "")
    which is always "" on the post-model_dump() dict (the field is flat
    body_storage, not nested).  Fixed code reads data.get("body_storage", "").
    """
    storage_html = "<p>Hello <strong>world</strong></p>"

    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "12345",
                "title": "Test Page",
                "type": "page",
                "body": {"storage": {"value": storage_html, "representation": "storage"}},
            },
        )
    )

    with patch("atlassian_skills.cli.confluence.load_config", return_value=Config()):
        result = _runner.invoke(
            app,
            ["--format=json", "confluence", "page", "get", "12345", "--body-repr=md"],
            env=_CONFLUENCE_ENV,
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    # After conversion, the body_storage field must contain Markdown, not raw XHTML
    body = data.get("body_storage", "")
    assert "<p>" not in body
    assert "Hello" in body


@respx.mock
def test_confluence_page_get_body_repr_md_empty_body_storage() -> None:
    """--body-repr=md with an empty body_storage must not crash and produce output."""
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/99999.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "99999",
                "title": "Empty Page",
                "type": "page",
                "body": {"storage": {"value": "", "representation": "storage"}},
            },
        )
    )

    with patch("atlassian_skills.cli.confluence.load_config", return_value=Config()):
        result = _runner.invoke(
            app,
            ["--format=json", "confluence", "page", "get", "99999", "--body-repr=md"],
            env=_CONFLUENCE_ENV,
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data.get("title") == "Empty Page"
    # body_storage stays empty string or None — must not crash
    assert data.get("body_storage", "") == ""


# ===========================================================================
# Bug #2 — cli/jira.py: json.loads JSONDecodeError → ValidationError (exit 7)
# ===========================================================================


@pytest.mark.parametrize(
    "sub_command",
    [
        ["jira", "issue", "create", "--project=PROJ", "--type=Task", "--summary=T"],
        ["jira", "issue", "update", "PROJ-1"],
        ["jira", "issue", "transition", "PROJ-1", "--transition-id=31"],
    ],
)
@respx.mock
def test_jira_invalid_fields_json_raises_validation_exit(
    sub_command: list[str],
) -> None:
    """Invalid --fields-json must exit with code 7 (VALIDATION), not crash."""
    # Mock any possible HTTP call so we never hit a real server
    respx.get(url__startswith=BASE_URL).mock(return_value=httpx.Response(200, json={}))
    respx.post(url__startswith=BASE_URL).mock(return_value=httpx.Response(200, json={}))
    respx.put(url__startswith=BASE_URL).mock(return_value=httpx.Response(200, json={}))

    result = _runner.invoke(
        app,
        sub_command + ["--fields-json", "not{json"],
        env=_JIRA_ENV,
    )

    assert result.exit_code == 7, f"Expected exit 7 (VALIDATION), got {result.exit_code}.\nOutput: {result.output}"


def test_jira_batch_create_invalid_json_file_raises_validation_exit(
    tmp_path: Path,
) -> None:
    """issue-batch create with an invalid JSON file must exit with code 7."""
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("not{json", encoding="utf-8")

    result = _runner.invoke(
        app,
        ["jira", "issue-batch", "create", f"--json-file={bad_json}"],
        env=_JIRA_ENV,
        catch_exceptions=False,
    )

    assert result.exit_code == 7, f"Expected exit 7 (VALIDATION), got {result.exit_code}.\nOutput: {result.output}"


# ===========================================================================
# Bug #3 — core/stdin.py: ValueError → ValidationError (3 sites)
# ===========================================================================


def test_read_body_no_source_raises_validation_error() -> None:
    """read_body() with neither body nor body_file must raise ValidationError.

    Old code raised ValueError; fixed code raises ValidationError.
    """
    with pytest.raises(ValidationError, match="required"):
        read_body()


def test_read_body_oversized_stdin_raises_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stdin content exceeding 10 MB must raise ValidationError, not ValueError."""
    oversized = "x" * (10 * 1024 * 1024 + 1)

    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(oversized))

    with pytest.raises(ValidationError, match="limit"):
        read_body(body_file="-")


def test_read_body_oversized_file_raises_validation_error(tmp_path: Path) -> None:
    """A body file exceeding 10 MB must raise ValidationError, not ValueError."""
    big_file = tmp_path / "big.txt"
    # Write slightly over the 10 MB limit
    big_file.write_bytes(b"z" * (10 * 1024 * 1024 + 1))

    with pytest.raises(ValidationError, match="limit"):
        read_body(body_file=str(big_file))


# ===========================================================================
# Bug #4 — core/client.py: retry budget exhaustion raises typed error
# ===========================================================================


@pytest.mark.parametrize(
    "status_code,expected_error",
    [
        (429, RateLimitError),
        (503, NetworkError),
    ],
)
@respx.mock
def test_retry_budget_exhaustion_raises_typed_error(
    status_code: int,
    expected_error: type,
) -> None:
    """After budget exhaustion, http_error_to_atlas must produce the right type.

    Old code raised a generic AtlasError after break; fixed code calls
    http_error_to_atlas(response.status_code, ...).
    """
    respx.get(f"{BASE_URL}/rest/api/2/search").mock(return_value=httpx.Response(status_code))

    # Exhaust budget instantly by making elapsed always >= max_total_retry_seconds
    _real_monotonic = time.monotonic
    call_count = 0

    def _fast_clock() -> float:
        nonlocal call_count
        call_count += 1
        # First call returns 0 (total_start), subsequent calls return 999
        return 0.0 if call_count == 1 else 999.0

    client = make_client(max_retries=3)
    with (
        patch("atlassian_skills.core.client.time.monotonic", side_effect=_fast_clock),
        pytest.raises(expected_error) as exc_info,
    ):
        client.get("/rest/api/2/search")

    assert exc_info.value.http_status == status_code


# ===========================================================================
# Bug #5 — core/client.py: absolute URL handling (no double-prefix)
# ===========================================================================


@respx.mock
def test_absolute_url_not_double_prefixed() -> None:
    """request() with an absolute URL must use it verbatim, not prepend base_url."""
    absolute = "https://other.example.com/some/path"
    route = respx.get(absolute).mock(return_value=httpx.Response(200, json={}))

    client = make_client()
    resp = client.request("GET", absolute)

    assert resp.status_code == 200
    assert route.called
    # Confirm the request URL is exactly the absolute URL, not double-prefixed
    assert str(route.calls[0].request.url) == absolute


@respx.mock
def test_relative_path_prepends_base_url() -> None:
    """request() with a relative path must prepend base_url exactly once."""
    route = respx.get(f"{BASE_URL}/rest/api/2/issue/PROJ-1").mock(
        return_value=httpx.Response(200, json={"key": "PROJ-1"})
    )

    client = make_client()
    resp = client.request("GET", "/rest/api/2/issue/PROJ-1")

    assert resp.status_code == 200
    assert route.called
    request_url = str(route.calls[0].request.url)
    # base_url must appear exactly once
    assert request_url.count(BASE_URL) == 1


# ===========================================================================
# Bug #6 — core/pagination.py: empty page guard & actual count advancement
# ===========================================================================


def test_paginate_offset_stops_on_empty_page_two() -> None:
    """paginate_offset must stop when page 2 returns an empty items list.

    Old code without an empty-page guard would loop forever (or until
    start_at >= total, which never changes when items is empty).
    The fix adds: if not items: break
    """
    call_count = 0

    def fetch(start_at: int, max_results: int) -> dict:
        nonlocal call_count
        call_count += 1
        if start_at == 0:
            return {"startAt": 0, "maxResults": 5, "total": 10, "issues": [{"id": i} for i in range(5)]}
        # Page 2 unexpectedly empty (server-side inconsistency)
        return {"startAt": 5, "maxResults": 5, "total": 10, "issues": []}

    pages = list(paginate_offset(fetch, max_results_per_page=5, items_key="issues"))

    assert call_count == 2, "Should have fetched exactly 2 pages before stopping"
    assert len(pages) == 2


def test_paginate_offset_actual_count_advances_start_at() -> None:
    """paginate_offset must use len(items) for advancement, not max_results_per_page.

    This ensures a short last page terminates correctly rather than
    advancing start_at past total.
    """
    calls: list[tuple[int, int]] = []

    def fetch(start_at: int, max_results: int) -> dict:
        calls.append((start_at, max_results))
        if start_at == 0:
            return {"startAt": 0, "maxResults": 5, "total": 7, "issues": [{"id": i} for i in range(5)]}
        # Last page: only 2 items remain
        return {"startAt": 5, "maxResults": 5, "total": 7, "issues": [{"id": 5}, {"id": 6}]}

    pages = list(paginate_offset(fetch, max_results_per_page=5, items_key="issues"))

    assert len(pages) == 2
    # Second call must start at 5 (actual count from page 1), not some other offset
    assert calls[1][0] == 5
