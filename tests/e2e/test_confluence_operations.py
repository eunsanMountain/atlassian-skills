from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from atlassian_skills.confluence.client import ConfluenceClient
from atlassian_skills.core.errors import AtlasError


@pytest.mark.integration
def test_e2e_confluence_get_page(e2e_confluence_client: ConfluenceClient, e2e_test_space: str) -> None:
    """Search for a page, then fetch it by ID."""
    result = e2e_confluence_client.search(f'type=page AND space="{e2e_test_space}"', limit=1)
    assert result.results, f"No pages found in space {e2e_test_space}"
    page_id = result.results[0].id
    page = e2e_confluence_client.get_page(page_id)
    assert page.id == page_id
    assert page.title


@pytest.mark.integration
def test_e2e_confluence_search(e2e_confluence_client: ConfluenceClient, e2e_test_space: str) -> None:
    """CQL search returns a ConfluenceSearchResult with metadata."""
    result = e2e_confluence_client.search(f'space="{e2e_test_space}"', limit=10)
    assert result.total >= 0
    assert isinstance(result.results, list)


@pytest.mark.integration
def test_e2e_confluence_create_update_delete(e2e_confluence_client: ConfluenceClient, e2e_test_space: str) -> None:
    """Full lifecycle: create page → update → delete."""
    created = e2e_confluence_client.create_page(
        space_key=e2e_test_space,
        title="[atlassian-skills e2e] create_update_delete",
        body="<p>initial content</p>",
    )
    page_id = created.get("id")
    assert page_id, f"create_page did not return an id: {created}"

    # Fetch to get current version number
    page = e2e_confluence_client.get_page(page_id)
    version_number = page.version.get("number", 1) if isinstance(page.version, dict) else 1

    e2e_confluence_client.update_page(
        page_id=page_id,
        title="[atlassian-skills e2e] create_update_delete (updated)",
        body="<p>updated content</p>",
        version_number=version_number + 1,
    )
    updated = e2e_confluence_client.get_page(page_id)
    assert "updated" in updated.title

    e2e_confluence_client.delete_page(page_id)
    with pytest.raises(AtlasError):
        e2e_confluence_client.get_page(page_id)


@pytest.mark.integration
def test_e2e_confluence_add_comment(e2e_confluence_client: ConfluenceClient, e2e_test_space: str) -> None:
    """Add a comment to a page and verify it is returned by list_comments."""
    result = e2e_confluence_client.search(f'type=page AND space="{e2e_test_space}"', limit=1)
    assert result.results, f"No pages in space {e2e_test_space}"
    page_id = result.results[0].id

    resp = e2e_confluence_client.add_comment(page_id, "<p>atlassian-skills e2e comment</p>")
    assert resp.get("id"), f"add_comment did not return an id: {resp}"

    comments = e2e_confluence_client.list_comments(page_id)
    assert any(c.id == resp["id"] for c in comments)


@pytest.mark.integration
def test_e2e_confluence_labels(e2e_confluence_client: ConfluenceClient, e2e_test_space: str) -> None:
    """Add a label to a page and verify it appears in list_labels."""
    result = e2e_confluence_client.search(f'type=page AND space="{e2e_test_space}"', limit=1)
    assert result.results, f"No pages in space {e2e_test_space}"
    page_id = result.results[0].id

    label_name = "atls-e2e-test"
    e2e_confluence_client.add_label(page_id, [label_name])

    labels = e2e_confluence_client.list_labels(page_id)
    assert any(lb.name == label_name for lb in labels)


@pytest.mark.integration
def test_e2e_confluence_space_tree(e2e_confluence_client: ConfluenceClient, e2e_test_space: str) -> None:
    """get_space_tree returns a SpaceTreeResult with pages."""
    tree = e2e_confluence_client.get_space_tree(e2e_test_space)
    assert tree.space_key == e2e_test_space
    assert tree.total_pages >= 0
    assert isinstance(tree.pages, list)


@pytest.mark.integration
def test_e2e_confluence_upload_attachment(e2e_confluence_client: ConfluenceClient, e2e_test_space: str) -> None:
    """Upload a small file attachment to a page and verify it appears in list_attachments."""
    result = e2e_confluence_client.search(f'type=page AND space="{e2e_test_space}"', limit=1)
    assert result.results, f"No pages in space {e2e_test_space}"
    page_id = result.results[0].id

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"atlassian-skills e2e attachment test")
        tmp_path = f.name

    try:
        resp = e2e_confluence_client.upload_attachment(page_id, tmp_path)
        assert resp is not None
        attachments = e2e_confluence_client.list_attachments(page_id)
        assert len(attachments) > 0
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@pytest.mark.integration
def test_e2e_confluence_push_pull_md(e2e_confluence_client: ConfluenceClient, e2e_test_space: str) -> None:
    """Create a page with storage body, read it back, verify body is non-empty."""
    created = e2e_confluence_client.create_page(
        space_key=e2e_test_space,
        title="[atlassian-skills e2e] push_pull_md",
        body="<p>Hello from <strong>atlassian-skills</strong> e2e test.</p>",
    )
    page_id = created.get("id")
    assert page_id, f"create_page did not return an id: {created}"

    try:
        page = e2e_confluence_client.get_page(page_id, include_body=True)
        assert page.id == page_id
        body_value = page.body.get("storage", {}).get("value", "") if isinstance(page.body, dict) else ""
        assert body_value, "Expected non-empty page body"
    finally:
        e2e_confluence_client.delete_page(page_id)
