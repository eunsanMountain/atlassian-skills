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

FIXTURES = Path(__file__).parent.parent / "fixtures" / "jira"
JIRA_URL = "https://jira.example.com"
runner = CliRunner()


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject URL + token for 'default' profile via environment variables."""
    monkeypatch.setenv("ATLS_DEFAULT_JIRA_URL", JIRA_URL)
    monkeypatch.setenv("ATLS_DEFAULT_JIRA_TOKEN", "test-pat-token")
    monkeypatch.setattr("atlassian_skills.cli.jira.load_config", lambda: Config())


@respx.mock
def test_issue_get_compact(snapshot: SnapshotAssertion) -> None:
    """atls jira issue get PROJ-3 --format=compact matches snapshot."""
    respx.get(f"{JIRA_URL}/rest/api/2/issue/PROJ-3").mock(
        return_value=httpx.Response(200, json=_load("get-issue-proj3.json"))
    )
    result = runner.invoke(app, ["jira", "issue", "get", "PROJ-3", "--format", "compact"])
    assert result.exit_code == 0, result.output
    assert result.output == snapshot


@respx.mock
def test_issue_get_json(snapshot: SnapshotAssertion) -> None:
    """atls jira issue get PROJ-3 --format=json matches snapshot."""
    respx.get(f"{JIRA_URL}/rest/api/2/issue/PROJ-3").mock(
        return_value=httpx.Response(200, json=_load("get-issue-proj3.json"))
    )
    result = runner.invoke(app, ["--format", "json", "jira", "issue", "get", "PROJ-3"])
    assert result.exit_code == 0, result.output
    assert result.output == snapshot


@respx.mock
def test_issue_search_compact(snapshot: SnapshotAssertion) -> None:
    """atls jira issue search 'project=PROJ' --format=compact matches snapshot."""
    respx.get(f"{JIRA_URL}/rest/api/2/search").mock(return_value=httpx.Response(200, json=_load("search-proj.json")))
    result = runner.invoke(app, ["--format", "compact", "jira", "issue", "search", "project=PROJ"])
    assert result.exit_code == 0, result.output
    assert result.output == snapshot
