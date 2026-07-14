from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import uuid4

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
@pytest.mark.usefixtures("e2e_allow_writes")
def test_e2e_confluence_create_update_delete(
    e2e_confluence_client: ConfluenceClient, e2e_test_space: str, e2e_test_parent: str | None
) -> None:
    """Full lifecycle: create page → update → delete."""
    suffix = uuid4().hex[:12]
    created = e2e_confluence_client.create_page(
        space_key=e2e_test_space,
        title=f"[atlassian-skills e2e] lifecycle {suffix}",
        body="<p>initial content</p>",
        ancestor_id=e2e_test_parent,
    )
    page_id = created.get("id")
    assert page_id, f"create_page did not return an id: {created}"

    deleted = False
    try:
        page = e2e_confluence_client.get_page(page_id)
        version_number = page.version.get("number", 1) if isinstance(page.version, dict) else 1

        e2e_confluence_client.update_page(
            page_id=page_id,
            title=f"[atlassian-skills e2e] lifecycle {suffix} (updated)",
            body="<p>updated content</p>",
            version_number=version_number + 1,
        )
        updated = e2e_confluence_client.get_page(page_id)
        assert "updated" in updated.title

        e2e_confluence_client.delete_page(page_id)
        deleted = True
        with pytest.raises(AtlasError):
            e2e_confluence_client.get_page(page_id)
    finally:
        if not deleted:
            e2e_confluence_client.delete_page(page_id)


@pytest.mark.integration
def test_e2e_confluence_add_comment(e2e_confluence_client: ConfluenceClient, e2e_temp_page: str) -> None:
    """Add a comment to our OWN throwaway page and verify it is returned by list_comments."""
    page_id = e2e_temp_page

    resp = e2e_confluence_client.add_comment(page_id, "<p>atlassian-skills e2e comment</p>")
    assert resp.get("id"), f"add_comment did not return an id: {resp}"

    comments = e2e_confluence_client.list_comments(page_id)
    assert any(c.id == resp["id"] for c in comments)


@pytest.mark.integration
def test_e2e_confluence_labels(e2e_confluence_client: ConfluenceClient, e2e_temp_page: str) -> None:
    """Add a label to our OWN throwaway page and verify it appears in list_labels."""
    page_id = e2e_temp_page

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
def test_e2e_confluence_upload_attachment(e2e_confluence_client: ConfluenceClient, e2e_temp_page: str) -> None:
    """Upload a small file attachment to our OWN throwaway page and verify it appears."""
    page_id = e2e_temp_page

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
@pytest.mark.usefixtures("e2e_allow_writes")
def test_e2e_confluence_push_pull_md(
    e2e_confluence_client: ConfluenceClient, e2e_test_space: str, e2e_test_parent: str | None
) -> None:
    """Create a page with storage body, read it back, verify body is non-empty."""
    created = e2e_confluence_client.create_page(
        space_key=e2e_test_space,
        title=f"[atlassian-skills e2e] push_pull_md {uuid4().hex[:12]}",
        body="<p>Hello from <strong>atlassian-skills</strong> e2e test.</p>",
        ancestor_id=e2e_test_parent,
    )
    page_id = created.get("id")
    assert page_id, f"create_page did not return an id: {created}"

    try:
        page = e2e_confluence_client.get_page(page_id, include_body=True)
        assert page.id == page_id
        body_value = page.body_storage or ""
        assert body_value, "Expected non-empty page body"
    finally:
        e2e_confluence_client.delete_page(page_id)
