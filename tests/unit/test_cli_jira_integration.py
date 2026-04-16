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

FIXTURES = Path(__file__).parent.parent / "fixtures" / "jira"
JIRA_URL = "https://jira.example.com"
runner = CliRunner()


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject URL + token for 'default' profile and isolate from real config file."""
    monkeypatch.setenv("ATLS_DEFAULT_JIRA_URL", JIRA_URL)
    monkeypatch.setenv("ATLS_DEFAULT_JIRA_TOKEN", "test-token")
    # Prevent the real ~/.config/atlassian-skills/config.toml from overriding URLs
    monkeypatch.setattr(
        "atlassian_skills.cli.jira.load_config",
        lambda: Config(),
    )


# ---------------------------------------------------------------------------
# Read commands
# ---------------------------------------------------------------------------


@respx.mock
def test_cli_jira_issue_get_compact() -> None:
    """atls jira issue get PROJ-3 returns exit 0 and shows key."""
    respx.get(f"{JIRA_URL}/rest/api/2/issue/PROJ-3").mock(
        return_value=httpx.Response(200, json=_load("get-issue-proj3.json"))
    )
    result = runner.invoke(app, ["jira", "issue", "get", "PROJ-3"])
    assert result.exit_code == 0, result.output
    assert "PROJ-3" in result.output


@respx.mock
def test_cli_jira_issue_get_json_format() -> None:
    """atls --format json jira issue get PROJ-3 returns valid JSON with key."""
    respx.get(f"{JIRA_URL}/rest/api/2/issue/PROJ-3").mock(
        return_value=httpx.Response(200, json=_load("get-issue-proj3.json"))
    )
    result = runner.invoke(app, ["--format", "json", "jira", "issue", "get", "PROJ-3"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["key"] == "PROJ-3"


@respx.mock
def test_cli_jira_issue_get_not_found_exit_code() -> None:
    """404 response maps to exit code 2 (NOT_FOUND)."""
    respx.get(f"{JIRA_URL}/rest/api/2/issue/PROJ-999").mock(
        return_value=httpx.Response(404, json={"message": "Issue does not exist"})
    )
    result = runner.invoke(app, ["jira", "issue", "get", "PROJ-999"])
    assert result.exit_code == ExitCode.NOT_FOUND


@respx.mock
def test_cli_jira_issue_get_auth_error_exit_code() -> None:
    """401 response maps to exit code 6 (AUTH)."""
    respx.get(f"{JIRA_URL}/rest/api/2/issue/PROJ-3").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorized"})
    )
    result = runner.invoke(app, ["jira", "issue", "get", "PROJ-3"])
    assert result.exit_code == ExitCode.AUTH


@respx.mock
def test_cli_jira_search_compact() -> None:
    """atls jira issue search 'project=PROJ' returns exit 0 and shows issue key."""
    respx.get(f"{JIRA_URL}/rest/api/2/search").mock(return_value=httpx.Response(200, json=_load("search-proj.json")))
    result = runner.invoke(app, ["jira", "issue", "search", "project=PROJ"])
    assert result.exit_code == 0, result.output
    assert "PROJ-3" in result.output


@respx.mock
def test_cli_jira_issue_get_requested_customfield_in_json() -> None:
    """Explicitly requested customfield_* keys are preserved in JSON output."""
    fixture = _load("get-issue-proj3.json")
    fixture.setdefault("fields", {})
    fixture["fields"]["customfield_10100"] = "PROJ-1"
    respx.get(f"{JIRA_URL}/rest/api/2/issue/PROJ-3").mock(return_value=httpx.Response(200, json=fixture))
    result = runner.invoke(
        app,
        ["jira", "issue", "get", "PROJ-3", "--fields", "summary,customfield_10100", "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["customfield_10100"] == "PROJ-1"


@respx.mock
def test_cli_jira_search_local_json_format_overrides_global() -> None:
    """Local -f json on issue search overrides the global format setting."""
    respx.get(f"{JIRA_URL}/rest/api/2/search").mock(return_value=httpx.Response(200, json=_load("search-proj.json")))
    result = runner.invoke(
        app,
        ["--format", "compact", "jira", "issue", "search", "project=PROJ", "-f", "json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data[0]["key"] == "PROJ-3"


@respx.mock
def test_cli_jira_project_list_local_json_format() -> None:
    """Local --format json works on commands that previously required the global flag."""
    respx.get(f"{JIRA_URL}/rest/api/2/project").mock(
        return_value=httpx.Response(200, json=_load("get-projects-sample.json"))
    )
    result = runner.invoke(app, ["jira", "project", "list", "--format", "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data[0]["key"] == "TEST"


@respx.mock
def test_cli_jira_projects_list() -> None:
    """atls jira project list returns exit 0 and lists project keys."""
    respx.get(f"{JIRA_URL}/rest/api/2/project").mock(
        return_value=httpx.Response(200, json=_load("get-projects-sample.json"))
    )
    result = runner.invoke(app, ["jira", "project", "list"])
    assert result.exit_code == 0, result.output
    assert "TEST" in result.output


# ---------------------------------------------------------------------------
# Write commands
# ---------------------------------------------------------------------------


@respx.mock
def test_cli_jira_issue_create_dry_run() -> None:
    """--dry-run on issue create shows POST preview and exits 0 without hitting API."""
    result = runner.invoke(
        app,
        ["jira", "issue", "create", "--project", "PROJ", "--type", "Bug", "--summary", "Test bug", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "POST" in result.output
    assert "PROJ" in result.output


@respx.mock
def test_cli_jira_issue_create_success() -> None:
    """issue create POSTs and returns the new issue key."""
    created = {"id": "123456", "key": "PROJ-42", "self": f"{JIRA_URL}/rest/api/2/issue/123456"}
    respx.post(f"{JIRA_URL}/rest/api/2/issue").mock(return_value=httpx.Response(201, json=created))
    result = runner.invoke(
        app,
        ["jira", "issue", "create", "--project", "PROJ", "--type", "Bug", "--summary", "Test bug"],
    )
    assert result.exit_code == 0, result.output
    assert "PROJ-42" in result.output


@respx.mock
def test_cli_jira_issue_update_dry_run() -> None:
    """--dry-run on issue update shows PUT preview without calling update endpoint."""
    result = runner.invoke(
        app,
        ["jira", "issue", "update", "PROJ-3", "--fields-json", '{"summary": "new title"}', "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "PUT" in result.output
    assert "PROJ-3" in result.output


@respx.mock
def test_cli_jira_issue_update_stale_check() -> None:
    """--if-updated mismatch exits with STALE (5)."""
    issue_data = dict(_load("get-issue-proj3.json"))
    issue_data["updated"] = "2026-04-10T18:21:25.303+0900"
    respx.get(f"{JIRA_URL}/rest/api/2/issue/PROJ-3").mock(return_value=httpx.Response(200, json=issue_data))
    result = runner.invoke(
        app,
        [
            "jira",
            "issue",
            "update",
            "PROJ-3",
            "--fields-json",
            '{"summary": "updated"}',
            "--if-updated",
            "2026-01-01T00:00:00.000+0000",
        ],
    )
    assert result.exit_code == ExitCode.STALE


def test_cli_jira_issue_update_invalid_fields_json() -> None:
    """Invalid --fields-json exits with VALIDATION (7)."""
    result = runner.invoke(
        app,
        ["jira", "issue", "update", "PROJ-3", "--fields-json", "not-json"],
    )
    assert result.exit_code == ExitCode.VALIDATION


@respx.mock
def test_cli_jira_issue_update_customfield_verification_success() -> None:
    """A verified customfield update exits 0 after the read-back check."""
    respx.put(f"{JIRA_URL}/rest/api/2/issue/PROJ-3").mock(return_value=httpx.Response(204))
    respx.get(f"{JIRA_URL}/rest/api/2/issue/PROJ-3").mock(
        return_value=httpx.Response(
            200,
            json={"id": "629816", "key": "PROJ-3", "fields": {"customfield_10100": "EPIC-1"}},
        )
    )
    result = runner.invoke(
        app,
        ["jira", "issue", "update", "PROJ-3", "--set-customfield", "customfield_10100=EPIC-1"],
    )
    assert result.exit_code == 0, result.output
    assert "updated" in result.output


@respx.mock
def test_cli_jira_issue_update_customfield_verification_failure() -> None:
    """A silent Jira no-op becomes a validation error instead of fake success."""
    respx.put(f"{JIRA_URL}/rest/api/2/issue/PROJ-3").mock(return_value=httpx.Response(204))
    respx.get(f"{JIRA_URL}/rest/api/2/issue/PROJ-3").mock(
        return_value=httpx.Response(
            200,
            json={"id": "629816", "key": "PROJ-3", "fields": {"customfield_10100": None}},
        )
    )
    result = runner.invoke(
        app,
        ["jira", "issue", "update", "PROJ-3", "--set-customfield", "customfield_10100=EPIC-1"],
    )
    assert result.exit_code == ExitCode.VALIDATION
    assert "Custom field update was not applied" in result.output


@respx.mock
def test_cli_jira_issue_transition() -> None:
    """issue transition POSTs to transitions endpoint and returns transitioned status."""
    respx.post(f"{JIRA_URL}/rest/api/2/issue/PROJ-3/transitions").mock(return_value=httpx.Response(204))
    result = runner.invoke(
        app,
        ["jira", "issue", "transition", "PROJ-3", "--transition-id", "11"],
    )
    assert result.exit_code == 0, result.output
    assert "transitioned" in result.output


@respx.mock
def test_cli_jira_issue_transition_dry_run() -> None:
    """--dry-run on transition shows POST preview without hitting API."""
    result = runner.invoke(
        app,
        ["jira", "issue", "transition", "PROJ-3", "--transition-id", "11", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "POST" in result.output
    assert "transitions" in result.output


@respx.mock
def test_cli_jira_issue_delete() -> None:
    """issue delete DELETEs and returns deleted status."""
    respx.delete(f"{JIRA_URL}/rest/api/2/issue/PROJ-3").mock(return_value=httpx.Response(204))
    result = runner.invoke(app, ["jira", "issue", "delete", "PROJ-3"])
    assert result.exit_code == 0, result.output
    assert "deleted" in result.output


@respx.mock
def test_cli_jira_issue_delete_dry_run() -> None:
    """--dry-run on delete shows DELETE preview without hitting API."""
    result = runner.invoke(app, ["jira", "issue", "delete", "PROJ-3", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "DELETE" in result.output
    assert "PROJ-3" in result.output


# ---------------------------------------------------------------------------
# Exit code matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "http_status,expected_exit",
    [
        (401, ExitCode.AUTH),
        (403, ExitCode.PERMISSION),
        (404, ExitCode.NOT_FOUND),
        (409, ExitCode.CONFLICT),
        (429, ExitCode.RATE_LIMITED),
        (500, ExitCode.NETWORK),
    ],
)
@respx.mock
def test_cli_jira_exit_codes(http_status: int, expected_exit: int) -> None:
    """HTTP status codes map to the correct CLI exit codes."""
    respx.get(f"{JIRA_URL}/rest/api/2/issue/PROJ-3").mock(
        return_value=httpx.Response(http_status, json={"message": "error"})
    )
    result = runner.invoke(app, ["jira", "issue", "get", "PROJ-3"])
    assert result.exit_code == expected_exit
