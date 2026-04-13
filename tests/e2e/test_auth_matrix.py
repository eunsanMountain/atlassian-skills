from __future__ import annotations

import os

import pytest

from atlassian_skills.confluence.client import ConfluenceClient
from atlassian_skills.core.auth import Credential
from atlassian_skills.jira.client import JiraClient


@pytest.mark.integration
def test_e2e_jira_pat_auth(e2e_jira_client: JiraClient) -> None:
    """PAT auth: list_projects succeeds and returns at least one project."""
    projects = e2e_jira_client.list_projects()
    assert isinstance(projects, list)
    assert len(projects) > 0


@pytest.mark.integration
def test_e2e_jira_basic_auth() -> None:
    """Basic auth: construct a client with username+password and verify connectivity."""
    url = os.environ.get("ATLS_E2E_JIRA_URL")
    user = os.environ.get("ATLS_E2E_JIRA_USER")
    password = os.environ.get("ATLS_E2E_JIRA_PASSWORD")
    if not (url and user and password):
        pytest.skip("Missing ATLS_E2E_JIRA_URL / ATLS_E2E_JIRA_USER / ATLS_E2E_JIRA_PASSWORD")
    cred = Credential(method="basic", token=password, username=user)
    client = JiraClient(base_url=url, credential=cred)
    projects = client.list_projects()
    assert isinstance(projects, list)


@pytest.mark.integration
def test_e2e_confluence_pat_auth(e2e_confluence_client: ConfluenceClient) -> None:
    """PAT auth: search returns without error."""
    result = e2e_confluence_client.search("type=page", limit=1)
    assert result.total >= 0


@pytest.mark.integration
def test_e2e_confluence_basic_auth() -> None:
    """Basic auth: construct a client with username+password and verify connectivity."""
    url = os.environ.get("ATLS_E2E_CONFLUENCE_URL")
    user = os.environ.get("ATLS_E2E_CONFLUENCE_USER")
    password = os.environ.get("ATLS_E2E_CONFLUENCE_PASSWORD")
    if not (url and user and password):
        pytest.skip(
            "Missing ATLS_E2E_CONFLUENCE_URL / ATLS_E2E_CONFLUENCE_USER / ATLS_E2E_CONFLUENCE_PASSWORD"
        )
    cred = Credential(method="basic", token=password, username=user)
    client = ConfluenceClient(base_url=url, credential=cred)
    result = client.search("type=page", limit=1)
    assert result.total >= 0
