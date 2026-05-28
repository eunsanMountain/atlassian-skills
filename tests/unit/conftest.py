from __future__ import annotations

import pytest

from atlassian_skills.confluence.client import ConfluenceClient
from atlassian_skills.core.auth import Credential
from atlassian_skills.core.client import BaseClient
from atlassian_skills.jira.client import JiraClient


@pytest.fixture
def bypass_tty_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `_ensure_interactive_terminal` a no-op for wizard tests.

    `CliRunner` runs commands with a non-TTY stdin, which would otherwise hit the
    wizard's TTY guard and exit before any prompt is exercised. Tests that
    specifically validate the guard skip this fixture.
    """
    import atlassian_skills.cli.setup as setup_mod

    monkeypatch.setattr(setup_mod, "_ensure_interactive_terminal", lambda: None)


@pytest.fixture
def base_client(mock_credential: Credential, jira_base_url: str) -> BaseClient:
    return BaseClient(jira_base_url, mock_credential)


@pytest.fixture
def jira_client(mock_credential: Credential, jira_base_url: str) -> JiraClient:
    return JiraClient(jira_base_url, mock_credential)


@pytest.fixture
def confluence_client(mock_credential: Credential, confluence_base_url: str) -> ConfluenceClient:
    return ConfluenceClient(confluence_base_url, mock_credential)
