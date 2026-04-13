from __future__ import annotations

import os

import pytest

from atlassian_skills.confluence.client import ConfluenceClient
from atlassian_skills.core.auth import Credential
from atlassian_skills.jira.client import JiraClient


def _skip_unless_env(*keys: str) -> None:
    for k in keys:
        if not os.environ.get(k):
            pytest.skip(f"Missing env var {k}")


@pytest.fixture
def e2e_jira_client() -> JiraClient:
    _skip_unless_env("ATLS_E2E_JIRA_URL", "ATLS_E2E_JIRA_TOKEN")
    cred = Credential(method="pat", token=os.environ["ATLS_E2E_JIRA_TOKEN"])
    return JiraClient(base_url=os.environ["ATLS_E2E_JIRA_URL"], credential=cred)


@pytest.fixture
def e2e_confluence_client() -> ConfluenceClient:
    _skip_unless_env("ATLS_E2E_CONFLUENCE_URL", "ATLS_E2E_CONFLUENCE_TOKEN")
    cred = Credential(method="pat", token=os.environ["ATLS_E2E_CONFLUENCE_TOKEN"])
    return ConfluenceClient(base_url=os.environ["ATLS_E2E_CONFLUENCE_URL"], credential=cred)


@pytest.fixture
def e2e_test_project() -> str:
    return os.environ.get("ATLS_E2E_PROJECT", "ATLSTEST")


@pytest.fixture
def e2e_test_space() -> str:
    return os.environ.get("ATLS_E2E_SPACE", "ATLSTEST")
