from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from atlassian_skills.cli.main import app
from atlassian_skills.core.config import Config
from atlassian_skills.core.errors import ExitCode

FIXTURES = Path(__file__).parent.parent / "fixtures" / "confluence"
CONFLUENCE_URL = "https://confluence.example.com"
runner = CliRunner()

# Raw Confluence REST API format for a Page (what the server actually returns)
_RAW_PAGE = {
    "id": "429140627",
    "title": "[PROJ-3] Navi Map 통합-경로 판단 개선",
    "type": "page",
    "status": "current",
    "space": {"key": "TESTSPACE", "name": "Test Lab", "type": "global"},
    "version": {"number": 2, "when": "2024-01-01T00:00:00.000Z"},
    "_links": {"webui": "/pages/viewpage.action?pageId=429140627"},
    "body": {
        "storage": {
            "value": "<p>Test content</p>",
            "representation": "storage",
        }
    },
}

_RAW_PAGE_CREATED = {
    "id": "123456789",
    "title": "Test Page",
    "type": "page",
    "status": "current",
    "space": {"key": "TESTSPACE", "name": "Test Lab", "type": "global"},
    "version": {"number": 1},
    "_links": {"webui": "/pages/viewpage.action?pageId=123456789"},
}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject URL + token for 'default' profile and isolate from real config file."""
    monkeypatch.setenv("ATLS_DEFAULT_CONFLUENCE_URL", CONFLUENCE_URL)
    monkeypatch.setenv("ATLS_DEFAULT_CONFLUENCE_TOKEN", "test-token")
    # Prevent the real ~/.config/atlassian-skills/config.toml from overriding URLs
    monkeypatch.setattr(
        "atlassian_skills.cli.confluence.load_config",
        lambda: Config(),
    )


# ---------------------------------------------------------------------------
# Read commands
# ---------------------------------------------------------------------------


@respx.mock
def test_cli_confluence_page_get_compact() -> None:
    """atls confluence page get <id> returns exit 0 and shows page title."""
    # Client appends ?expand=... so match by URL regex to handle query params
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/429140627").mock(
        return_value=httpx.Response(200, json=_RAW_PAGE)
    )
    result = runner.invoke(app, ["confluence", "page", "get", "429140627"])
    assert result.exit_code == 0, result.output


@respx.mock
def test_cli_confluence_page_get_json_format() -> None:
    """--format json on page get returns valid JSON with id."""
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/429140627").mock(
        return_value=httpx.Response(200, json=_RAW_PAGE)
    )
    result = runner.invoke(app, ["--format", "json", "confluence", "page", "get", "429140627"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["id"] == "429140627"


@respx.mock
def test_cli_confluence_page_get_body_repr_md() -> None:
    """--body-repr md fetches full page with body expansion and returns exit 0."""
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/429140627").mock(
        return_value=httpx.Response(200, json=_RAW_PAGE)
    )
    result = runner.invoke(app, ["confluence", "page", "get", "429140627", "--body-repr", "md"])
    assert result.exit_code == 0, result.output


@respx.mock
def test_cli_confluence_page_get_not_found() -> None:
    """404 on page get exits with NOT_FOUND (2)."""
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/999999").mock(
        return_value=httpx.Response(404, json={"message": "Not found"})
    )
    result = runner.invoke(app, ["confluence", "page", "get", "999999"])
    assert result.exit_code == ExitCode.NOT_FOUND


@respx.mock
def test_cli_confluence_page_search() -> None:
    """atls confluence page search <cql> returns exit 0."""
    search_response = {
        "results": [_RAW_PAGE],
        "start": 0,
        "limit": 25,
        "size": 1,
        "totalSize": 1,
        "_links": {},
    }
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/search").mock(
        return_value=httpx.Response(200, json=search_response)
    )
    result = runner.invoke(app, ["confluence", "page", "search", "space=TESTSPACE"])
    assert result.exit_code == 0, result.output


@respx.mock
def test_cli_confluence_page_search_local_json_format() -> None:
    """Local --format json on page search works after the subcommand."""
    search_response = {
        "results": [_RAW_PAGE],
        "start": 0,
        "limit": 25,
        "size": 1,
        "totalSize": 1,
        "_links": {},
    }
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/search").mock(
        return_value=httpx.Response(200, json=search_response)
    )
    result = runner.invoke(app, ["confluence", "page", "search", "space=TESTSPACE", "--format", "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data[0]["id"] == "429140627"


@respx.mock
def test_cli_confluence_comments_list() -> None:
    """atls confluence comment list <page_id> returns exit 0."""
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/429140627/child/comment").mock(
        return_value=httpx.Response(200, json=_load("get-comments-sample.json"))
    )
    result = runner.invoke(app, ["confluence", "comment", "list", "429140627"])
    assert result.exit_code == 0, result.output


@respx.mock
def test_cli_confluence_labels_list() -> None:
    """atls confluence label list <page_id> returns exit 0 and includes label data."""
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/429140627/label").mock(
        return_value=httpx.Response(200, json=_load("get-labels-sample.json"))
    )
    result = runner.invoke(app, ["--format", "json", "confluence", "label", "list", "429140627"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    names = [item.get("name") for item in data]
    assert "architecture" in names


# ---------------------------------------------------------------------------
# Write commands
# ---------------------------------------------------------------------------


@respx.mock
def test_cli_confluence_page_create_dry_run(tmp_path: Path) -> None:
    """--dry-run on page create shows POST preview without hitting API."""
    body_file = tmp_path / "body.html"
    body_file.write_text("<p>Hello world</p>")
    result = runner.invoke(
        app,
        [
            "confluence",
            "page",
            "create",
            "--space",
            "TESTSPACE",
            "--title",
            "Test Page",
            "--body-file",
            str(body_file),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "POST" in result.output
    assert "TESTSPACE" in result.output


@respx.mock
def test_cli_confluence_page_create_success(tmp_path: Path) -> None:
    """page create POSTs and returns created page id."""
    body_file = tmp_path / "body.html"
    body_file.write_text("<p>Hello world</p>")
    respx.post(f"{CONFLUENCE_URL}/rest/api/content").mock(
        return_value=httpx.Response(200, json=_RAW_PAGE_CREATED)
    )
    result = runner.invoke(
        app,
        [
            "--format",
            "json",
            "confluence",
            "page",
            "create",
            "--space",
            "TESTSPACE",
            "--title",
            "Test Page",
            "--body-file",
            str(body_file),
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["id"] == "123456789"


@respx.mock
def test_cli_confluence_page_update(tmp_path: Path) -> None:
    """page update fetches current version then PUTs new content."""
    body_file = tmp_path / "body.html"
    body_file.write_text("<p>Updated content</p>")
    # GET is called with query params by the client
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/429140627").mock(
        return_value=httpx.Response(200, json=_RAW_PAGE)
    )
    respx.put(f"{CONFLUENCE_URL}/rest/api/content/429140627").mock(
        return_value=httpx.Response(200, json=_RAW_PAGE)
    )
    result = runner.invoke(
        app,
        ["confluence", "page", "update", "429140627", "--body-file", str(body_file)],
    )
    assert result.exit_code == 0, result.output


@respx.mock
def test_cli_confluence_page_update_dry_run(tmp_path: Path) -> None:
    """--dry-run on page update fetches page then shows PUT preview."""
    body_file = tmp_path / "body.html"
    body_file.write_text("<p>Draft content</p>")
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/429140627").mock(
        return_value=httpx.Response(200, json=_RAW_PAGE)
    )
    result = runner.invoke(
        app,
        ["confluence", "page", "update", "429140627", "--body-file", str(body_file), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "PUT" in result.output


@respx.mock
def test_cli_confluence_page_delete() -> None:
    """page delete DELETEs and exits 0."""
    respx.delete(f"{CONFLUENCE_URL}/rest/api/content/429140627").mock(
        return_value=httpx.Response(204)
    )
    result = runner.invoke(app, ["confluence", "page", "delete", "429140627"])
    assert result.exit_code == 0, result.output


@respx.mock
def test_cli_confluence_page_delete_json_format() -> None:
    """page delete with --format json outputs deleted id."""
    respx.delete(f"{CONFLUENCE_URL}/rest/api/content/429140627").mock(
        return_value=httpx.Response(204)
    )
    result = runner.invoke(app, ["--format", "json", "confluence", "page", "delete", "429140627"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["deleted"] == "429140627"


@respx.mock
def test_cli_confluence_page_delete_dry_run() -> None:
    """--dry-run on page delete shows DELETE preview without hitting API."""
    result = runner.invoke(app, ["confluence", "page", "delete", "429140627", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "DELETE" in result.output
    assert "429140627" in result.output


@respx.mock
def test_cli_confluence_page_push_md_reads_stdin_and_skips_missing_asset_dir(tmp_path: Path) -> None:
    """push-md accepts --md-file=- and treats a missing asset dir as empty."""
    current_page = dict(_RAW_PAGE)
    current_page["body"] = {"storage": {"value": "<p>Old content</p>", "representation": "storage"}}
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/429140627").mock(
        return_value=httpx.Response(200, json=current_page)
    )
    missing_assets = tmp_path / "assets"
    result = runner.invoke(
        app,
        [
            "--quiet",
            "confluence",
            "page",
            "push-md",
            "429140627",
            "--md-file",
            "-",
            "--asset-dir",
            str(missing_assets),
            "--dry-run",
            "--format",
            "json",
        ],
        input="# Updated from stdin\n",
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "dry_run"
    assert data["would_update"] is True


# ---------------------------------------------------------------------------
# Exit code matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "http_status,expected_exit",
    [
        (401, ExitCode.AUTH),
        (403, ExitCode.PERMISSION),
        (404, ExitCode.NOT_FOUND),
        (500, ExitCode.NETWORK),
    ],
)
@respx.mock
def test_cli_confluence_exit_codes(http_status: int, expected_exit: int) -> None:
    """HTTP status codes map to the correct CLI exit codes."""
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/429140627").mock(
        return_value=httpx.Response(http_status, json={"message": "error"})
    )
    result = runner.invoke(app, ["confluence", "page", "get", "429140627"])
    assert result.exit_code == expected_exit
