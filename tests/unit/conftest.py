from __future__ import annotations

import pytest

from atlassian_skills.confluence.client import ConfluenceClient
from atlassian_skills.core.auth import Credential
from atlassian_skills.core.client import BaseClient
from atlassian_skills.jira.client import JiraClient


@pytest.fixture
def base_client(mock_credential: Credential, jira_base_url: str) -> BaseClient:
    return BaseClient(jira_base_url, mock_credential)


@pytest.fixture
def jira_client(mock_credential: Credential, jira_base_url: str) -> JiraClient:
    return JiraClient(jira_base_url, mock_credential)


@pytest.fixture
def confluence_client(mock_credential: Credential, confluence_base_url: str) -> ConfluenceClient:
    return ConfluenceClient(confluence_base_url, mock_credential)
