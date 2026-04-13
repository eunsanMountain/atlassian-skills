from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from syrupy.assertion import SnapshotAssertion
from typer.testing import CliRunner

from atlassian_skills.cli.main import app
from atlassian_skills.core.config import Config

FIXTURES = Path(__file__).parent.parent / "fixtures" / "confluence"
CONFLUENCE_URL = "https://confluence.example.com"
runner = CliRunner()


def _load(name: str) -> dict | list:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject URL + token for 'default' profile via environment variables."""
    monkeypatch.setenv("ATLS_DEFAULT_CONFLUENCE_URL", CONFLUENCE_URL)
    monkeypatch.setenv("ATLS_DEFAULT_CONFLUENCE_TOKEN", "test-pat-token")
    monkeypatch.setattr("atlassian_skills.cli.confluence.load_config", lambda: Config())


@respx.mock
def test_confluence_page_compact(snapshot: SnapshotAssertion) -> None:
    """atls confluence page get 429140627 --format=compact matches snapshot."""
    respx.get(f"{CONFLUENCE_URL}/rest/api/content/429140627").mock(
        return_value=httpx.Response(200, json=_load("get-page-sample.json")["metadata"])
    )
    result = runner.invoke(app, ["confluence", "page", "get", "429140627", "--format", "compact"])
    assert result.exit_code == 0, result.output
    assert result.output == snapshot


@respx.mock
def test_confluence_page_json_format(snapshot: SnapshotAssertion) -> None:
    """atls --format=json confluence page get 429140627 matches snapshot."""
    respx.get(f"{CONFLUENCE_URL}/rest/api/content/429140627").mock(
        return_value=httpx.Response(200, json=_load("get-page-sample.json")["metadata"])
    )
    result = runner.invoke(app, ["--format", "json", "confluence", "page", "get", "429140627"])
    assert result.exit_code == 0, result.output
    assert result.output == snapshot


@respx.mock
def test_confluence_search_compact(snapshot: SnapshotAssertion) -> None:
    """atls confluence page search 'space=TESTSPACE' --format=compact matches snapshot."""
    fixture = _load("search-proj.json")
    respx.get(f"{CONFLUENCE_URL}/rest/api/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": fixture,
                "start": 0,
                "limit": 25,
                "size": len(fixture),
                "_links": {},
            },
        )
    )
    result = runner.invoke(app, ["--format", "compact", "confluence", "page", "search", "space=TESTSPACE"])
    assert result.exit_code == 0, result.output
    assert result.output == snapshot


@respx.mock
def test_confluence_search_json_format(snapshot: SnapshotAssertion) -> None:
    """atls --format=json confluence page search 'space=TESTSPACE' matches snapshot."""
    fixture = _load("search-proj.json")
    respx.get(f"{CONFLUENCE_URL}/rest/api/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": fixture,
                "start": 0,
                "limit": 25,
                "size": len(fixture),
                "_links": {},
            },
        )
    )
    result = runner.invoke(app, ["--format", "json", "confluence", "page", "search", "space=TESTSPACE"])
    assert result.exit_code == 0, result.output
    assert result.output == snapshot


@respx.mock
def test_confluence_page_not_found_error(snapshot: SnapshotAssertion) -> None:
    """atls --format=json confluence page get 999 returns JSON error on 404."""
    respx.get(f"{CONFLUENCE_URL}/rest/api/content/999").mock(
        return_value=httpx.Response(404, json={"message": "Page not found"})
    )
    result = runner.invoke(app, ["--format", "json", "confluence", "page", "get", "999"])
    assert result.exit_code != 0
    assert result.output == snapshot
