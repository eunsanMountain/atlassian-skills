from __future__ import annotations

import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from atlassian_skills.cli.main import app
from atlassian_skills.core.config import Config
from atlassian_skills.core.errors import ExitCode

ZEPHYR_URL = "https://jira.example.com"
API = "/rest/atm/1.0"
runner = CliRunner()


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLS_DEFAULT_ZEPHYR_URL", ZEPHYR_URL)
    monkeypatch.setenv("ATLS_DEFAULT_ZEPHYR_TOKEN", "test-token")
    monkeypatch.setattr("atlassian_skills.cli.zephyr.load_config", lambda: Config())


@respx.mock
def test_cli_zephyr_testcase_get_json() -> None:
    respx.get(f"{ZEPHYR_URL}{API}/testcase/JQA-T1").mock(
        return_value=httpx.Response(200, json={"key": "JQA-T1", "name": "Login", "projectKey": "JQA"})
    )

    result = runner.invoke(app, ["zephyr", "testcase", "get", "JQA-T1", "--format", "json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["key"] == "JQA-T1"


@respx.mock
def test_cli_zephyr_search_compact() -> None:
    respx.get(f"{ZEPHYR_URL}{API}/testcase/search").mock(
        return_value=httpx.Response(200, json=[{"key": "JQA-T1", "name": "Login", "projectKey": "JQA"}])
    )

    result = runner.invoke(app, ["zephyr", "testcase", "search", "--query", 'projectKey = "JQA"'])

    assert result.exit_code == 0, result.output
    assert "JQA-T1" in result.output


@respx.mock
def test_cli_zephyr_create_dry_run_does_not_hit_api() -> None:
    route = respx.post(f"{ZEPHYR_URL}{API}/testcase").mock(return_value=httpx.Response(500))

    result = runner.invoke(
        app,
        [
            "zephyr",
            "testcase",
            "create",
            "--data-json",
            '{"name":"Login","projectKey":"JQA"}',
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "POST" in result.output
    assert "Login" in result.output
    assert not route.called


def test_cli_zephyr_invalid_data_json_exits_validation() -> None:
    result = runner.invoke(app, ["zephyr", "testcase", "create", "--data-json", "not-json"])

    assert result.exit_code == ExitCode.VALIDATION


@respx.mock
def test_cli_zephyr_testrun_results_json() -> None:
    respx.get(f"{ZEPHYR_URL}{API}/testrun/JQA-R1/testresults").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 7, "testCaseKey": "JQA-T1", "projectKey": "JQA", "status": "Pass"}],
        )
    )

    result = runner.invoke(app, ["zephyr", "testrun", "results", "JQA-R1", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["id"] == 7


@respx.mock
def test_cli_zephyr_environment_list() -> None:
    respx.get(f"{ZEPHYR_URL}{API}/environments").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 1, "name": "QA"}]})
    )

    result = runner.invoke(app, ["zephyr", "environment", "list", "JQA", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["name"] == "QA"


@respx.mock
def test_cli_zephyr_not_found_exit_code() -> None:
    respx.get(f"{ZEPHYR_URL}{API}/testcase/MISSING").mock(return_value=httpx.Response(404, text="missing"))

    result = runner.invoke(app, ["zephyr", "testcase", "get", "MISSING"])

    assert result.exit_code == ExitCode.NOT_FOUND
