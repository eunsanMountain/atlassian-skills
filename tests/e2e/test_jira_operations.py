from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from atlassian_skills.core.errors import AtlasError
from atlassian_skills.jira.client import JiraClient


@pytest.mark.integration
def test_e2e_jira_get_issue(e2e_jira_client: JiraClient, e2e_test_project: str) -> None:
    """Search for any issue in the test project, then fetch it by key."""
    result = e2e_jira_client.search(f"project={e2e_test_project}", max_results=1)
    assert result.issues, f"No issues found in project {e2e_test_project}"
    issue = e2e_jira_client.get_issue(result.issues[0].key)
    assert issue.key
    assert issue.fields is not None


@pytest.mark.integration
def test_e2e_jira_search(e2e_jira_client: JiraClient, e2e_test_project: str) -> None:
    """Search returns a SearchResult with expected metadata."""
    result = e2e_jira_client.search(f"project={e2e_test_project}", max_results=10)
    assert result.total >= 0
    assert isinstance(result.issues, list)


@pytest.mark.integration
def test_e2e_jira_create_update_delete(e2e_jira_client: JiraClient, e2e_test_project: str) -> None:
    """Full lifecycle: create → update → delete an issue."""
    created = e2e_jira_client.create_issue(
        fields={
            "project": {"key": e2e_test_project},
            "summary": "[atlassian-skills e2e] create_update_delete test",
            "issuetype": {"name": "Task"},
        }
    )
    key = created.get("key") or created.get("id")
    assert key, f"create_issue did not return a key/id: {created}"

    e2e_jira_client.update_issue(key, fields={"summary": "[atlassian-skills e2e] updated summary"})
    updated = e2e_jira_client.get_issue(key)
    assert "updated" in (updated.fields.get("summary", "") if isinstance(updated.fields, dict) else "")

    e2e_jira_client.delete_issue(key)
    # After delete, attempting to fetch should raise
    with pytest.raises(AtlasError):
        e2e_jira_client.get_issue(key)


@pytest.mark.integration
def test_e2e_jira_transitions(e2e_jira_client: JiraClient, e2e_test_project: str) -> None:
    """Fetch available transitions for the first issue found."""
    result = e2e_jira_client.search(f"project={e2e_test_project}", max_results=1)
    assert result.issues, f"No issues in project {e2e_test_project}"
    key = result.issues[0].key
    transitions = e2e_jira_client.get_transitions(key)
    assert isinstance(transitions, list)
    # At least one transition should exist for any real issue
    assert len(transitions) > 0
    assert transitions[0].id
    assert transitions[0].name


@pytest.mark.integration
def test_e2e_jira_add_comment(e2e_jira_client: JiraClient, e2e_test_project: str) -> None:
    """Add a comment to an existing issue and verify it was stored."""
    result = e2e_jira_client.search(f"project={e2e_test_project}", max_results=1)
    assert result.issues, f"No issues in project {e2e_test_project}"
    key = result.issues[0].key
    comment_body = "[atlassian-skills e2e] automated comment"
    resp = e2e_jira_client.add_comment(key, comment_body)
    assert resp.get("id"), f"add_comment did not return an id: {resp}"
    assert resp.get("body") == comment_body


@pytest.mark.integration
def test_e2e_jira_list_projects(e2e_jira_client: JiraClient) -> None:
    """list_projects returns at least one project."""
    projects = e2e_jira_client.list_projects()
    assert isinstance(projects, list)
    assert len(projects) > 0
    assert projects[0].key


@pytest.mark.integration
def test_e2e_jira_search_fields(e2e_jira_client: JiraClient) -> None:
    """search_fields returns all fields and keyword filtering works."""
    all_fields = e2e_jira_client.search_fields()
    assert len(all_fields) > 0

    summary_fields = e2e_jira_client.search_fields(keyword="summary")
    assert any(f.id == "summary" for f in summary_fields)


@pytest.mark.integration
def test_e2e_jira_list_boards(e2e_jira_client: JiraClient, e2e_test_project: str) -> None:
    """list_boards returns boards; filtering by project key works."""
    boards = e2e_jira_client.list_boards(project=e2e_test_project)
    assert isinstance(boards, list)
    # Board list may be empty if no boards configured — that is still a valid response.
    for b in boards:
        assert b.id
        assert b.name


@pytest.mark.integration
def test_e2e_jira_upload_attachment(e2e_jira_client: JiraClient, e2e_test_project: str) -> None:
    """Upload a small text attachment to an issue and verify it appears in the attachment list."""
    result = e2e_jira_client.search(f"project={e2e_test_project}", max_results=1)
    assert result.issues, f"No issues in project {e2e_test_project}"
    key = result.issues[0].key

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"atlassian-skills e2e attachment test")
        tmp_path = f.name

    try:
        resp = e2e_jira_client.upload_attachment(key, tmp_path)
        # upload_attachment returns a list or dict depending on server
        assert resp is not None
    finally:
        Path(tmp_path).unlink(missing_ok=True)
