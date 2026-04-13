from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from atlassian_skills.confluence.client import ConfluenceClient
from atlassian_skills.confluence.models import (
    ConfluenceSearchResult,
    Page,
    SpaceTreeResult,
)
from atlassian_skills.core.auth import Credential
from atlassian_skills.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "confluence"
BASE_URL = "https://confluence.example.com"


def _load(name: str) -> dict | list:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def cred() -> Credential:
    return Credential(method="pat", token="test-token")


@pytest.fixture
def client(cred: Credential) -> ConfluenceClient:
    return ConfluenceClient(BASE_URL, cred)


# ---------------------------------------------------------------------------
# get_page — read paths
# ---------------------------------------------------------------------------


@respx.mock
def test_get_page_default_expand(client: ConfluenceClient) -> None:
    # Use inline raw API format (the fixture file uses preprocessed format)
    fixture = {
        "id": "429140627",
        "title": "Test Page",
        "type": "page",
        "status": "current",
        "version": {"number": 1},
        "space": {"key": "IVSL", "name": "IVS Lab"},
        "body": {"storage": {"value": "<p>content</p>", "representation": "storage"}},
    }
    route = respx.get(f"{BASE_URL}/rest/api/content/429140627").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    page = client.get_page("429140627")

    assert isinstance(page, Page)
    called_params = route.calls.last.request.url.params
    assert "body.storage" in called_params["expand"]
    assert "version" in called_params["expand"]


@respx.mock
def test_get_page_without_body(client: ConfluenceClient) -> None:
    fixture = {
        "id": "100",
        "title": "Lightweight",
        "type": "page",
        "version": {"number": 1},
        "space": {"key": "TS", "name": "Test Space"},
    }
    route = respx.get(f"{BASE_URL}/rest/api/content/100").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    page = client.get_page("100", include_body=False)

    called_params = route.calls.last.request.url.params
    assert "body" not in called_params["expand"]
    assert page.id == "100"


@respx.mock
def test_get_page_with_ancestors(client: ConfluenceClient) -> None:
    fixture = {
        "id": "200",
        "title": "Child Page",
        "type": "page",
        "status": "current",
        "version": {"number": 3},
        "space": {"key": "DEMO", "name": "Demo Space"},
        "ancestors": [
            {"id": "100", "title": "Parent Page", "type": "page"},
            {"id": "50", "title": "Root Page", "type": "page"},
        ],
        "body": {"storage": {"value": "<p>content</p>", "representation": "storage"}},
    }
    respx.get(f"{BASE_URL}/rest/api/content/200").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    page = client.get_page("200")

    assert page.ancestors is not None
    assert len(page.ancestors) == 2
    assert page.ancestors[0].title == "Parent Page"
    assert page.ancestors[1].id == "50"


@respx.mock
def test_get_page_404_raises_not_found(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/content/9999").mock(
        return_value=httpx.Response(404, text="Page not found")
    )

    with pytest.raises(NotFoundError):
        client.get_page("9999")


@respx.mock
def test_get_page_version_number(client: ConfluenceClient) -> None:
    fixture = {
        "id": "300",
        "title": "Versioned Page",
        "type": "page",
        "version": {"number": 7, "when": "2024-03-01T00:00:00.000Z"},
        "body": {"storage": {"value": "<p>v7</p>", "representation": "storage"}},
    }
    respx.get(f"{BASE_URL}/rest/api/content/300").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    page = client.get_page("300")

    assert page.version is not None
    assert page.version.number == 7  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@respx.mock
def test_search_with_cql(client: ConfluenceClient) -> None:
    fixture = {
        "results": [
            {"id": "1", "title": "Design Doc", "type": "page"},
            {"id": "2", "title": "API Guide", "type": "page"},
        ],
        "start": 0,
        "limit": 25,
        "size": 2,
        "_links": {},
    }
    route = respx.get(f"{BASE_URL}/rest/api/search").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    result = client.search("space=DEMO AND type=page")

    assert isinstance(result, ConfluenceSearchResult)
    assert len(result.results) == 2
    called_params = route.calls.last.request.url.params
    assert "space=DEMO" in called_params["cql"]


@respx.mock
def test_search_empty_results(client: ConfluenceClient) -> None:
    fixture: dict = {"results": [], "start": 0, "limit": 25, "size": 0, "_links": {}}
    respx.get(f"{BASE_URL}/rest/api/search").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    result = client.search("title='nonexistent page xyz'")

    assert isinstance(result, ConfluenceSearchResult)
    assert result.results == []
    assert result.total == 0


@respx.mock
def test_search_pagination_follows_next_link(client: ConfluenceClient) -> None:
    """search() uses get_paginated_links which follows _links.next."""
    page1 = {
        "results": [{"id": "1", "title": "Page A", "type": "page"}],
        "start": 0,
        "limit": 10,
        "size": 1,
        "_links": {"next": "/rest/api/search?cql=type%3Dpage&limit=10&start=1"},
    }
    page2 = {
        "results": [{"id": "2", "title": "Page B", "type": "page"}],
        "start": 1,
        "limit": 10,
        "size": 1,
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/search").mock(
        side_effect=[
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
        ]
    )

    result = client.search("type=page", limit=10)

    assert len(result.results) == 2
    titles = {p.title for p in result.results}
    assert "Page A" in titles
    assert "Page B" in titles


# ---------------------------------------------------------------------------
# get_children
# ---------------------------------------------------------------------------


@respx.mock
def test_get_children_returns_pages(client: ConfluenceClient) -> None:
    fixture: dict = {
        "results": [
            {"id": "10", "title": "Sub A", "type": "page"},
            {"id": "11", "title": "Sub B", "type": "page"},
            {"id": "12", "title": "Sub C", "type": "page"},
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/content/100/child/page").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    children = client.get_children("100")

    assert len(children) == 3
    assert all(isinstance(p, Page) for p in children)
    assert children[0].id == "10"
    assert children[2].title == "Sub C"


@respx.mock
def test_get_children_empty(client: ConfluenceClient) -> None:
    fixture: dict = {"results": [], "_links": {}}
    respx.get(f"{BASE_URL}/rest/api/content/100/child/page").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    children = client.get_children("100")

    assert children == []


# ---------------------------------------------------------------------------
# get_space_tree
# ---------------------------------------------------------------------------


@respx.mock
def test_get_space_tree_returns_result(client: ConfluenceClient) -> None:
    # Use raw API format with results key (the fixture file is preprocessed format)
    fixture = {
        "results": [
            {"id": "1", "title": "Root Page", "type": "page"},
            {"id": "2", "title": "Child Page", "type": "page"},
            {"id": "3", "title": "Another Page", "type": "page"},
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/space/IVSL/content").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    result = client.get_space_tree("IVSL")

    assert isinstance(result, SpaceTreeResult)
    assert result.space_key == "IVSL"
    assert result.total_pages == 3
    assert len(result.pages) == 3


@respx.mock
def test_get_space_tree_pagination(client: ConfluenceClient) -> None:
    page1: dict = {
        "results": [
            {"id": "1", "title": "Page 1", "type": "page"},
            {"id": "2", "title": "Page 2", "type": "page"},
        ],
        "_links": {"next": "/rest/api/space/TEST/content?limit=2&start=2"},
    }
    page2: dict = {
        "results": [
            {"id": "3", "title": "Page 3", "type": "page"},
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/space/TEST/content").mock(
        side_effect=[
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
        ]
    )

    result = client.get_space_tree("TEST")

    assert result.total_pages == 3


# ---------------------------------------------------------------------------
# get_page_history
# ---------------------------------------------------------------------------


@respx.mock
def test_get_page_history_specific_version(client: ConfluenceClient) -> None:
    fixture = {
        "id": "500",
        "title": "Historic Page",
        "type": "page",
        "version": {"number": 2, "when": "2024-02-01T00:00:00.000Z"},
        "body": {"storage": {"value": "<p>v2 content</p>", "representation": "storage"}},
    }
    route = respx.get(f"{BASE_URL}/rest/api/content/500").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    page = client.get_page_history("500", version=2)

    assert isinstance(page, Page)
    assert page.version.number == 2  # type: ignore[union-attr]
    called_params = route.calls.last.request.url.params
    assert called_params["status"] == "historical"
    assert called_params["version"] == "2"


@respx.mock
def test_get_page_history_version_not_found(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/content/500").mock(
        return_value=httpx.Response(404, text="Version not found")
    )

    with pytest.raises(NotFoundError):
        client.get_page_history("500", version=999)


# ---------------------------------------------------------------------------
# get_page_diff
# ---------------------------------------------------------------------------


@respx.mock
def test_get_page_diff_different_versions(client: ConfluenceClient) -> None:
    v1 = {
        "id": "600",
        "title": "Diff Page",
        "type": "page",
        "body": {"storage": {"value": "line1\noriginal\nline3", "representation": "storage"}},
        "version": {"number": 1},
    }
    v2 = {
        "id": "600",
        "title": "Diff Page",
        "type": "page",
        "body": {"storage": {"value": "line1\nmodified\nline3", "representation": "storage"}},
        "version": {"number": 2},
    }
    respx.get(f"{BASE_URL}/rest/api/content/600").mock(
        side_effect=[
            httpx.Response(200, json=v1),
            httpx.Response(200, json=v2),
        ]
    )

    diff = client.get_page_diff("600", from_ver=1, to_ver=2)

    assert "---" in diff
    assert "+++" in diff
    assert "-original" in diff
    assert "+modified" in diff


@respx.mock
def test_get_page_diff_identical_versions(client: ConfluenceClient) -> None:
    same_fixture = {
        "id": "601",
        "title": "Same Page",
        "type": "page",
        "body": {"storage": {"value": "unchanged content", "representation": "storage"}},
        "version": {"number": 1},
    }
    respx.get(f"{BASE_URL}/rest/api/content/601").mock(
        side_effect=[
            httpx.Response(200, json=same_fixture),
            httpx.Response(200, json=same_fixture),
        ]
    )

    diff = client.get_page_diff("601", from_ver=1, to_ver=1)

    assert diff == ""


@respx.mock
def test_get_page_diff_version_not_found(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/content/602").mock(
        return_value=httpx.Response(404, text="Not found")
    )

    with pytest.raises(NotFoundError):
        client.get_page_diff("602", from_ver=1, to_ver=5)


# ---------------------------------------------------------------------------
# get_page_images
# ---------------------------------------------------------------------------


@respx.mock
def test_get_page_images_only_images_returned(client: ConfluenceClient) -> None:
    fixture: dict = {
        "results": [
            {"id": "att1", "title": "photo.jpg", "mediaType": "image/jpeg", "fileSize": 1000},
            {"id": "att2", "title": "doc.pdf", "mediaType": "application/pdf", "fileSize": 5000},
            {"id": "att3", "title": "chart.png", "mediaType": "image/png", "fileSize": 2000},
            {"id": "att4", "title": "data.csv", "mediaType": "text/csv", "fileSize": 300},
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/content/700/child/attachment").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    images = client.get_page_images("700")

    assert len(images) == 2
    titles = {a.title for a in images}
    assert "photo.jpg" in titles
    assert "chart.png" in titles
    assert "doc.pdf" not in titles


@respx.mock
def test_get_page_images_no_images(client: ConfluenceClient) -> None:
    fixture: dict = {
        "results": [
            {"id": "att1", "title": "spec.docx", "mediaType": "application/docx", "fileSize": 10000},
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/content/701/child/attachment").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    images = client.get_page_images("701")

    assert images == []


# ---------------------------------------------------------------------------
# create_page — write error paths
# ---------------------------------------------------------------------------


@respx.mock
def test_create_page_400_validation_error(client: ConfluenceClient) -> None:
    respx.post(f"{BASE_URL}/rest/api/content").mock(
        return_value=httpx.Response(400, json={"message": "Title is required"})
    )

    with pytest.raises(ValidationError):
        client.create_page("TEST", "", "<p>body</p>")


@respx.mock
def test_create_page_missing_space(client: ConfluenceClient) -> None:
    """400 when space key does not exist."""
    respx.post(f"{BASE_URL}/rest/api/content").mock(
        return_value=httpx.Response(400, json={"message": "No space found for key NOPE"})
    )

    with pytest.raises(ValidationError):
        client.create_page("NOPE", "My Page", "<p>content</p>")


@respx.mock
def test_create_page_success(client: ConfluenceClient) -> None:
    resp_body = {
        "id": "800",
        "title": "New Page",
        "type": "page",
        "space": {"key": "TEST", "name": "Test Space"},
        "_links": {"webui": "/spaces/TEST/pages/800/New+Page"},
    }
    respx.post(f"{BASE_URL}/rest/api/content").mock(
        return_value=httpx.Response(200, json=resp_body)
    )

    result = client.create_page("TEST", "New Page", "<p>Hello</p>")

    assert result["id"] == "800"
    assert result["title"] == "New Page"


# ---------------------------------------------------------------------------
# update_page — write error paths
# ---------------------------------------------------------------------------


@respx.mock
def test_update_page_409_conflict(client: ConfluenceClient) -> None:
    """409 when version is stale (optimistic concurrency failure)."""
    respx.put(f"{BASE_URL}/rest/api/content/900").mock(
        return_value=httpx.Response(
            409,
            json={"message": "Version mismatch: expected 5, got 3"},
        )
    )

    with pytest.raises(ConflictError):
        client.update_page("900", "Updated Title", "<p>new body</p>", version_number=3)


@respx.mock
def test_update_page_missing_title(client: ConfluenceClient) -> None:
    respx.put(f"{BASE_URL}/rest/api/content/901").mock(
        return_value=httpx.Response(400, json={"message": "Title must not be empty"})
    )

    with pytest.raises(ValidationError):
        client.update_page("901", "", "<p>body</p>", version_number=1)


@respx.mock
def test_update_page_success(client: ConfluenceClient) -> None:
    resp_body = {
        "id": "902",
        "title": "Updated Title",
        "type": "page",
        "version": {"number": 4},
    }
    respx.put(f"{BASE_URL}/rest/api/content/902").mock(
        return_value=httpx.Response(200, json=resp_body)
    )

    result = client.update_page("902", "Updated Title", "<p>new</p>", version_number=4)

    assert result["id"] == "902"
    assert result["version"]["number"] == 4


# ---------------------------------------------------------------------------
# delete_page — write error paths
# ---------------------------------------------------------------------------


@respx.mock
def test_delete_page_404_not_found(client: ConfluenceClient) -> None:
    respx.delete(f"{BASE_URL}/rest/api/content/999").mock(
        return_value=httpx.Response(404, text="Page not found")
    )

    with pytest.raises(NotFoundError):
        client.delete_page("999")


@respx.mock
def test_delete_page_403_permission_denied(client: ConfluenceClient) -> None:
    respx.delete(f"{BASE_URL}/rest/api/content/800").mock(
        return_value=httpx.Response(403, text="Forbidden")
    )

    with pytest.raises(ForbiddenError):
        client.delete_page("800")


@respx.mock
def test_delete_page_success(client: ConfluenceClient) -> None:
    respx.delete(f"{BASE_URL}/rest/api/content/801").mock(
        return_value=httpx.Response(204)
    )

    client.delete_page("801")  # Should not raise


# ---------------------------------------------------------------------------
# move_page — write error paths
# ---------------------------------------------------------------------------


@respx.mock
def test_move_page_invalid_position(client: ConfluenceClient) -> None:
    respx.post(
        f"{BASE_URL}/rest/api/content/100/move/invalid/target/200"
    ).mock(
        return_value=httpx.Response(400, json={"message": "Invalid position: invalid"})
    )

    with pytest.raises(ValidationError):
        client.move_page("100", "invalid", "200")


@respx.mock
def test_move_page_success(client: ConfluenceClient) -> None:
    resp_body = {"id": "100", "title": "Moved Page", "type": "page"}
    respx.post(f"{BASE_URL}/rest/api/content/100/move/append/target/200").mock(
        return_value=httpx.Response(200, json=resp_body)
    )

    result = client.move_page("100", "append", "200")

    assert result["id"] == "100"


# ---------------------------------------------------------------------------
# upload_attachment — write error paths
# ---------------------------------------------------------------------------


def test_upload_attachment_file_not_found(client: ConfluenceClient, tmp_path: Path) -> None:
    nonexistent = tmp_path / "no_such_file.txt"

    with pytest.raises(FileNotFoundError):
        client.upload_attachment("100", nonexistent)


@respx.mock
def test_upload_attachment_success(client: ConfluenceClient, tmp_path: Path) -> None:
    test_file = tmp_path / "test.txt"
    test_file.write_text("hello world")

    resp_body = [{"id": "att200", "title": "test.txt"}]
    respx.post(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(
        return_value=httpx.Response(200, json=resp_body)
    )

    result = client.upload_attachment("100", test_file)

    assert result[0]["id"] == "att200"


# ---------------------------------------------------------------------------
# upload_attachments_batch — partial failure
# ---------------------------------------------------------------------------


@respx.mock
def test_upload_attachments_batch_all_succeed(client: ConfluenceClient, tmp_path: Path) -> None:
    file1 = tmp_path / "a.txt"
    file2 = tmp_path / "b.txt"
    file1.write_text("aaa")
    file2.write_text("bbb")

    # Mock list_attachments (skip check) — no existing attachments
    respx.get(f"{BASE_URL}/rest/api/content/500/child/attachment").mock(
        return_value=httpx.Response(200, json={"results": [], "_links": {}})
    )
    respx.post(f"{BASE_URL}/rest/api/content/500/child/attachment").mock(
        return_value=httpx.Response(200, json=[{"id": "att1"}])
    )

    results = client.upload_attachments_batch("500", [file1, file2])

    assert len(results) == 2


@respx.mock
def test_upload_attachments_batch_skip_existing(client: ConfluenceClient, tmp_path: Path) -> None:
    file1 = tmp_path / "existing.txt"
    file2 = tmp_path / "new.txt"
    file1.write_text("old content")
    file2.write_text("new content")

    # Mock existing attachments — file1 already exists
    respx.get(f"{BASE_URL}/rest/api/content/600/child/attachment").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "att_old", "title": "existing.txt", "mediaType": "text/plain", "fileSize": 100}
                ],
                "_links": {},
            },
        )
    )
    respx.post(f"{BASE_URL}/rest/api/content/600/child/attachment").mock(
        return_value=httpx.Response(200, json=[{"id": "att_new"}])
    )

    results = client.upload_attachments_batch("600", [file1, file2], if_exists="skip")

    # result[0] is the skipped dict {"title": ..., "skipped": True}
    # result[1] is the raw API response (a list from multipart upload)
    assert len(results) == 2
    skipped = [r for r in results if isinstance(r, dict) and r.get("skipped")]
    assert len(skipped) == 1
    assert skipped[0]["title"] == "existing.txt"
    not_skipped = [r for r in results if not (isinstance(r, dict) and r.get("skipped"))]
    assert len(not_skipped) == 1
