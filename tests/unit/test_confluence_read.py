from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from atlassian_skills.confluence.client import ConfluenceClient
from atlassian_skills.confluence.models import (
    Attachment,
    Comment,
    ConfluenceSearchResult,
    Label,
    Page,
    SpaceTreeResult,
)
from atlassian_skills.core.auth import Credential
from atlassian_skills.jira.models import User

FIXTURES = Path(__file__).parent.parent / "fixtures" / "confluence"
BASE_URL = "https://confluence.example.com"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load(name: str) -> dict | list:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def cred() -> Credential:
    return Credential(method="pat", token="test-token")


@pytest.fixture
def client(cred: Credential) -> ConfluenceClient:
    return ConfluenceClient(BASE_URL, cred)


# ---------------------------------------------------------------------------
# get_page
# ---------------------------------------------------------------------------


@respx.mock
def test_get_page_returns_page(client: ConfluenceClient) -> None:
    fixture = {
        "id": "429140627",
        "title": "[RLM-3] Navi Map 통합-경로 판단 개선",
        "type": "page",
        "status": "current",
        "space": {"key": "IVSL", "name": "IVS Lab"},
        "version": {"number": 2, "when": "2026-04-01T10:00:00.000+0900"},
        "body": {"storage": {"value": "<p>test</p>", "representation": "storage"}},
    }
    respx.get(f"{BASE_URL}/rest/api/content/429140627").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    page = client.get_page("429140627")

    assert isinstance(page, Page)
    assert page.id == "429140627"
    assert page.title == "[RLM-3] Navi Map 통합-경로 판단 개선"
    assert page.space is not None
    assert page.space.key == "IVSL"


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@respx.mock
def test_search_returns_results(client: ConfluenceClient) -> None:
    fixture = {
        "results": [
            {"id": "1", "title": "Page 1", "type": "page"},
            {"id": "2", "title": "Page 2", "type": "page"},
        ],
        "start": 0,
        "limit": 25,
        "size": 2,
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/search").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    result = client.search("type=page AND text~'test'")

    assert isinstance(result, ConfluenceSearchResult)
    assert len(result.results) == 2
    assert result.results[0].id == "1"
    assert result.results[1].title == "Page 2"


# ---------------------------------------------------------------------------
# get_children
# ---------------------------------------------------------------------------


@respx.mock
def test_get_children_returns_pages(client: ConfluenceClient) -> None:
    fixture = {
        "results": [
            {"id": "10", "title": "Child 1", "type": "page"},
            {"id": "11", "title": "Child 2", "type": "page"},
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/content/100/child/page").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    children = client.get_children("100")

    assert len(children) == 2
    assert all(isinstance(p, Page) for p in children)
    assert children[0].id == "10"


# ---------------------------------------------------------------------------
# list_comments
# ---------------------------------------------------------------------------


@respx.mock
def test_list_comments_returns_comments(client: ConfluenceClient) -> None:
    fixture = {
        "results": [
            {
                "id": "500",
                "title": "Re: Page",
                "type": "comment",
                "version": {"number": 1},
            },
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/content/100/child/comment").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    comments = client.list_comments("100")

    assert len(comments) == 1
    assert isinstance(comments[0], Comment)
    assert comments[0].id == "500"


# ---------------------------------------------------------------------------
# list_labels
# ---------------------------------------------------------------------------


@respx.mock
def test_list_labels_returns_labels(client: ConfluenceClient) -> None:
    fixture = {
        "results": [
            {"id": "1", "name": "important", "prefix": "global"},
            {"id": "2", "name": "draft", "prefix": "global"},
        ],
    }
    respx.get(f"{BASE_URL}/rest/api/content/100/label").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    labels = client.list_labels("100")

    assert len(labels) == 2
    assert all(isinstance(lb, Label) for lb in labels)
    assert labels[0].name == "important"


# ---------------------------------------------------------------------------
# list_attachments
# ---------------------------------------------------------------------------


@respx.mock
def test_list_attachments_returns_attachments(client: ConfluenceClient) -> None:
    fixture = {
        "results": [
            {
                "id": "att100",
                "title": "diagram.png",
                "mediaType": "image/png",
                "fileSize": 12345,
                "_links": {"download": "/download/attachments/100/diagram.png"},
            },
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    attachments = client.list_attachments("100")

    assert len(attachments) == 1
    assert isinstance(attachments[0], Attachment)
    assert attachments[0].title == "diagram.png"
    assert attachments[0].media_type == "image/png"
    assert attachments[0].file_size == 12345


# ---------------------------------------------------------------------------
# get_page_images
# ---------------------------------------------------------------------------


@respx.mock
def test_get_page_images_filters_images(client: ConfluenceClient) -> None:
    fixture = {
        "results": [
            {"id": "att1", "title": "photo.jpg", "mediaType": "image/jpeg", "fileSize": 1000},
            {"id": "att2", "title": "doc.pdf", "mediaType": "application/pdf", "fileSize": 2000},
            {"id": "att3", "title": "chart.png", "mediaType": "image/png", "fileSize": 3000},
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    images = client.get_page_images("100")

    assert len(images) == 2
    assert images[0].title == "photo.jpg"
    assert images[1].title == "chart.png"


# ---------------------------------------------------------------------------
# get_page_diff
# ---------------------------------------------------------------------------


@respx.mock
def test_get_page_diff_returns_unified_diff(client: ConfluenceClient) -> None:
    v1_fixture = {
        "id": "100",
        "title": "Test Page",
        "type": "page",
        "body": {"storage": {"value": "line1\nline2\nline3", "representation": "storage"}},
        "version": {"number": 1},
    }
    v2_fixture = {
        "id": "100",
        "title": "Test Page",
        "type": "page",
        "body": {"storage": {"value": "line1\nmodified\nline3", "representation": "storage"}},
        "version": {"number": 2},
    }
    # get_page_history calls get with status=historical
    route = respx.get(f"{BASE_URL}/rest/api/content/100")
    route.side_effect = [
        httpx.Response(200, json=v1_fixture),
        httpx.Response(200, json=v2_fixture),
    ]

    diff = client.get_page_diff("100", 1, 2)

    assert "---" in diff
    assert "+++" in diff
    assert "-line2" in diff
    assert "+modified" in diff


# ---------------------------------------------------------------------------
# search_users
# ---------------------------------------------------------------------------


@respx.mock
def test_search_users_fuzzy_match(client: ConfluenceClient) -> None:
    fixture = {
        "results": [
            {"displayName": "Alice Kim", "name": "alice.kim", "emailAddress": "alice@corp.com", "key": "U1"},
            {"displayName": "Bob Lee", "name": "bob.lee", "emailAddress": "bob@corp.com", "key": "U2"},
            {"displayName": "Charlie Alice", "name": "charlie", "emailAddress": "charlie@corp.com", "key": "U3"},
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/group/confluence-users/member").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    users = client.search_users("alice")

    assert len(users) == 2
    assert all(isinstance(u, User) for u in users)
    names = {u.name for u in users}
    assert "alice.kim" in names


# ---------------------------------------------------------------------------
# get_space_tree
# ---------------------------------------------------------------------------


@respx.mock
def test_get_space_tree_returns_result(client: ConfluenceClient) -> None:
    fixture = {
        "results": [
            {"id": "1", "title": "Root Page", "type": "page"},
            {"id": "2", "title": "Child Page", "type": "page"},
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/space/IVSL/content").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    result = client.get_space_tree("IVSL")

    assert isinstance(result, SpaceTreeResult)
    assert result.space_key == "IVSL"
    assert result.total_pages == 2
    assert len(result.pages) == 2


# ---------------------------------------------------------------------------
# download_attachment
# ---------------------------------------------------------------------------


@respx.mock
def test_download_attachment(client: ConfluenceClient, tmp_path: Path) -> None:
    content = b"file-content-bytes"
    respx.get(f"{BASE_URL}/rest/api/content/att100/download").mock(
        return_value=httpx.Response(200, content=content)
    )

    out = client.download_attachment("att100", tmp_path / "test.bin")

    assert out.exists()
    assert out.read_bytes() == content


# ---------------------------------------------------------------------------
# download_all_attachments
# ---------------------------------------------------------------------------


@respx.mock
def test_download_all_attachments(client: ConfluenceClient, tmp_path: Path) -> None:
    # Mock list attachments
    list_fixture = {
        "results": [
            {"id": "att1", "title": "file1.txt", "mediaType": "text/plain", "fileSize": 100},
            {"id": "att2", "title": "file2.txt", "mediaType": "text/plain", "fileSize": 200},
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(
        return_value=httpx.Response(200, json=list_fixture)
    )
    respx.get(f"{BASE_URL}/rest/api/content/att1/download").mock(
        return_value=httpx.Response(200, content=b"content1")
    )
    respx.get(f"{BASE_URL}/rest/api/content/att2/download").mock(
        return_value=httpx.Response(200, content=b"content2")
    )

    paths = client.download_all_attachments("100", tmp_path)

    assert len(paths) == 2
    assert (tmp_path / "file1.txt").read_bytes() == b"content1"
    assert (tmp_path / "file2.txt").read_bytes() == b"content2"


# ---------------------------------------------------------------------------
# get_page_history
# ---------------------------------------------------------------------------


@respx.mock
def test_get_page_history_returns_page(client: ConfluenceClient) -> None:
    fixture = _load("get-page-history-v1.json")
    route = respx.get(f"{BASE_URL}/rest/api/content/429140627").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    page = client.get_page_history("429140627", version=1)

    req = route.calls[0].request
    assert req.url.params["status"] == "historical"
    assert req.url.params["version"] == "1"
    assert isinstance(page, Page)
    assert page.id == "429140627"


@respx.mock
def test_get_page_history_specific_version(client: ConfluenceClient) -> None:
    page_data = {
        "id": "100",
        "title": "My Page v3",
        "type": "page",
        "status": "historical",
        "version": {"number": 3, "when": "2024-03-01T00:00:00.000Z"},
        "body": {"storage": {"value": "<p>Version 3 content</p>", "representation": "storage"}},
    }
    route = respx.get(f"{BASE_URL}/rest/api/content/100").mock(
        return_value=httpx.Response(200, json=page_data)
    )

    page = client.get_page_history("100", version=3)

    req = route.calls[0].request
    assert req.url.params["version"] == "3"
    assert isinstance(page, Page)
    assert page.title == "My Page v3"
    assert page.body_storage == "<p>Version 3 content</p>"


# ---------------------------------------------------------------------------
# get_page_images — via list_attachments (already covered via fixture approach)
# ---------------------------------------------------------------------------


@respx.mock
def test_get_page_images_only_image_types(client: ConfluenceClient) -> None:
    fixture = {
        "results": [
            {"id": "att10", "title": "banner.jpg", "mediaType": "image/jpeg", "fileSize": 50000},
            {"id": "att11", "title": "data.csv", "mediaType": "text/csv", "fileSize": 1000},
            {"id": "att12", "title": "logo.svg", "mediaType": "image/svg+xml", "fileSize": 2000},
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/content/200/child/attachment").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    images = client.get_page_images("200")

    assert len(images) == 2
    titles = {img.title for img in images}
    assert "banner.jpg" in titles
    assert "logo.svg" in titles
    assert "data.csv" not in titles


# ---------------------------------------------------------------------------
# get_children — verifies endpoint and page count
# ---------------------------------------------------------------------------


@respx.mock
def test_get_children_empty(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/content/999/child/page").mock(
        return_value=httpx.Response(200, json={"results": [], "_links": {}})
    )

    children = client.get_children("999")

    assert children == []


@respx.mock
def test_get_children_returns_correct_ids(client: ConfluenceClient) -> None:
    fixture = {
        "results": [
            {"id": "50", "title": "Sub Page A", "type": "page"},
            {"id": "51", "title": "Sub Page B", "type": "page"},
            {"id": "52", "title": "Sub Page C", "type": "page"},
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/content/300/child/page").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    children = client.get_children("300")

    assert len(children) == 3
    ids = [p.id for p in children]
    assert "50" in ids
    assert "52" in ids


# ---------------------------------------------------------------------------
# get_space_tree — using fixture
# ---------------------------------------------------------------------------


@respx.mock
def test_get_space_tree_fixture(client: ConfluenceClient) -> None:
    fixture = _load("get-space-tree-ivsl.json")
    # get_space_tree uses get_paginated_links which expects "results" key in API response
    # The fixture is pre-processed format; wrap its pages into the raw API envelope
    api_response = {
        "results": fixture["pages"],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/space/IVSL/content").mock(
        return_value=httpx.Response(200, json=api_response)
    )

    result = client.get_space_tree("IVSL")

    assert isinstance(result, SpaceTreeResult)
    assert result.space_key == "IVSL"
    assert result.total_pages == len(fixture["pages"])
    assert len(result.pages) == len(fixture["pages"])
    assert result.pages[0].title == "01. [통합 인지] Sensor Fusion Architecture"


# ---------------------------------------------------------------------------
# list_attachments — verifies endpoint
# ---------------------------------------------------------------------------


@respx.mock
def test_list_attachments_empty(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/content/400/child/attachment").mock(
        return_value=httpx.Response(200, json={"results": [], "_links": {}})
    )

    attachments = client.list_attachments("400")

    assert attachments == []


@respx.mock
def test_list_attachments_multiple_types(client: ConfluenceClient) -> None:
    fixture = {
        "results": [
            {"id": "att20", "title": "spec.pdf", "mediaType": "application/pdf", "fileSize": 102400},
            {"id": "att21", "title": "screenshot.png", "mediaType": "image/png", "fileSize": 4096},
            {"id": "att22", "title": "data.xlsx", "mediaType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "fileSize": 8192},
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/content/500/child/attachment").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    attachments = client.list_attachments("500")

    assert len(attachments) == 3
    assert all(isinstance(a, Attachment) for a in attachments)
    assert attachments[0].title == "spec.pdf"
    assert attachments[1].media_type == "image/png"


# ---------------------------------------------------------------------------
# download_attachment — verifies file content and path
# ---------------------------------------------------------------------------


@respx.mock
def test_download_attachment_returns_correct_path(client: ConfluenceClient, tmp_path: Path) -> None:
    content = b"binary file content here"
    respx.get(f"{BASE_URL}/rest/api/content/att500/download").mock(
        return_value=httpx.Response(200, content=content)
    )

    out = client.download_attachment("att500", tmp_path / "output.bin")

    assert out == tmp_path / "output.bin"
    assert out.read_bytes() == content


@respx.mock
def test_download_attachment_creates_parent_dirs(client: ConfluenceClient, tmp_path: Path) -> None:
    content = b"file content"
    respx.get(f"{BASE_URL}/rest/api/content/att600/download").mock(
        return_value=httpx.Response(200, content=content)
    )

    nested_path = tmp_path / "a" / "b" / "c" / "file.txt"
    out = client.download_attachment("att600", nested_path)

    assert out.exists()
    assert out.read_bytes() == content


# ---------------------------------------------------------------------------
# download_all_attachments — empty page
# ---------------------------------------------------------------------------


@respx.mock
def test_download_all_attachments_empty_page(client: ConfluenceClient, tmp_path: Path) -> None:
    respx.get(f"{BASE_URL}/rest/api/content/999/child/attachment").mock(
        return_value=httpx.Response(200, json={"results": [], "_links": {}})
    )

    paths = client.download_all_attachments("999", tmp_path)

    assert paths == []


# ---------------------------------------------------------------------------
# search_users — verifies fuzzy filtering
# ---------------------------------------------------------------------------


@respx.mock
def test_search_users_returns_empty_when_no_match(client: ConfluenceClient) -> None:
    fixture = {
        "results": [
            {"displayName": "Alice Kim", "name": "alice.kim", "emailAddress": "alice@corp.com", "key": "U1"},
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/group/confluence-users/member").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    users = client.search_users("zzz_no_such_user")

    assert users == []


@respx.mock
def test_search_users_matches_by_email(client: ConfluenceClient) -> None:
    fixture = {
        "results": [
            {"displayName": "Bob Lee", "name": "bob.lee", "emailAddress": "bob@corp.com", "key": "U2"},
            {"displayName": "Alice Kim", "name": "alice.kim", "emailAddress": "alice@corp.com", "key": "U1"},
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/group/confluence-users/member").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    users = client.search_users("bob@corp.com")

    assert len(users) == 1
    assert users[0].name == "bob.lee"
