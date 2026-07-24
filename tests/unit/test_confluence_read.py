from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock

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
from atlassian_skills.core.attachment_io import AttachmentWriter, AttachmentWriterKind
from atlassian_skills.core.auth import Credential
from atlassian_skills.core.directory_capability import DirectoryCapability
from atlassian_skills.core.errors import AtlasError
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
        "id": "12345678",
        "title": "[PROJ-3] 검색 결과 정렬 개선",
        "type": "page",
        "status": "current",
        "space": {"key": "TESTSPACE", "name": "Test Lab"},
        "version": {"number": 2, "when": "2026-04-01T10:00:00.000+0900"},
        "body": {"storage": {"value": "<p>test</p>", "representation": "storage"}},
    }
    respx.get(f"{BASE_URL}/rest/api/content/12345678").mock(return_value=httpx.Response(200, json=fixture))

    page = client.get_page("12345678")

    assert isinstance(page, Page)
    assert page.id == "12345678"
    assert page.title == "[PROJ-3] 검색 결과 정렬 개선"
    assert page.space is not None
    assert page.space.key == "TESTSPACE"


@respx.mock
def test_get_page_extracts_server_rendered_view(client: ConfluenceClient) -> None:
    fixture = {
        "id": "12345678",
        "title": "Rendered",
        "body": {"view": {"value": "<p>server HTML</p>", "representation": "view"}},
    }
    respx.get(f"{BASE_URL}/rest/api/content/12345678").mock(return_value=httpx.Response(200, json=fixture))

    page = client.get_page("12345678", expand="body.view")

    assert page.body_view == "<p>server HTML</p>"


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
    respx.get(f"{BASE_URL}/rest/api/search").mock(return_value=httpx.Response(200, json=fixture))

    result = client.search("type=page AND text~'test'")

    assert isinstance(result, ConfluenceSearchResult)
    assert len(result.results) == 2
    assert result.results[0].id == "1"
    assert result.results[1].title == "Page 2"


@respx.mock
def test_search_skips_non_content_results(client: ConfluenceClient) -> None:
    """Regression for GitHub #14.

    /rest/api/search is a universal CQL search whose results mix content,
    space, and user entities (e.g. a broad ``siteSearch ~ "..."`` matches user
    profiles and spaces). Only content results carry an ``id``; space/user
    results must be skipped instead of crashing Page validation.
    """
    fixture = {
        "results": [
            {
                "content": {"id": "111", "title": "Sample Page", "type": "page"},
                "entityType": "content",
                "title": "Sample Page",
                "url": "/display/DEMO/Sample+Page",
                "timestamp": 1700000000000,
            },
            {
                # space result: no "content" wrapper, no top-level "id"
                "space": {"id": 222, "key": "DEMO", "name": "Demo Space", "type": "global"},
                "entityType": "space",
                "title": "Demo Space",
                "url": "/spaces/DEMO",
                "timestamp": 1700000001000,
            },
            {
                # user result: shape that triggered #14 (has "user", no "id")
                "user": {
                    "type": "known",
                    "username": "jdoe",
                    "userKey": "0000aaaa1111bbbb2222cccc",
                    "displayName": "Jane Doe",
                },
                "entityType": "user",
                "title": "Jane Doe",
                "url": "/display/~jdoe",
                "timestamp": 1700000002000,
            },
            {
                "content": {"id": "333", "title": "Another Page", "type": "page"},
                "entityType": "content",
                "title": "Another Page",
                "url": "/display/DEMO/Another+Page",
                "timestamp": 1700000003000,
            },
        ],
        "start": 0,
        "limit": 25,
        "size": 4,
        "totalSize": 4,
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/search").mock(return_value=httpx.Response(200, json=fixture))

    # Must not raise pydantic ValidationError on the space/user entries.
    result = client.search('siteSearch ~ "demo"')

    assert isinstance(result, ConfluenceSearchResult)
    # Only the two content results survive; space + user are skipped.
    assert [p.id for p in result.results] == ["111", "333"]
    assert all(p.type == "page" for p in result.results)
    # total reflects the server-side match count, not the parsed subset.
    assert result.total == 4


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
    respx.get(f"{BASE_URL}/rest/api/content/100/child/page").mock(return_value=httpx.Response(200, json=fixture))

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
    respx.get(f"{BASE_URL}/rest/api/content/100/child/comment").mock(return_value=httpx.Response(200, json=fixture))

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
    respx.get(f"{BASE_URL}/rest/api/content/100/label").mock(return_value=httpx.Response(200, json=fixture))

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
    respx.get(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(return_value=httpx.Response(200, json=fixture))

    attachments = client.list_attachments("100")

    assert len(attachments) == 1
    assert isinstance(attachments[0], Attachment)
    assert attachments[0].title == "diagram.png"
    assert attachments[0].media_type == "image/png"
    assert attachments[0].file_size == 12345


def test_list_attachments_default_does_not_truncate_after_fifty(client: ConfluenceClient) -> None:
    raw_attachments = [
        {
            "id": f"att{index}",
            "title": f"attachment-{index}.bin",
            "mediaType": "application/octet-stream",
            "fileSize": index,
            "_links": {"download": f"/download/attachments/100/attachment-{index}.bin"},
        }
        for index in range(81)
    ]
    paginated = MagicMock(return_value=raw_attachments)
    client.get_paginated_links = paginated

    attachments = client.list_attachments("100")

    assert len(attachments) == 81
    paginated.assert_called_once_with(
        "/rest/api/content/100/child/attachment",
        params={"limit": 200, "expand": "version,extensions.mediaType,extensions.fileSize"},
        items_key="results",
        limit=None,
    )


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
    respx.get(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(return_value=httpx.Response(200, json=fixture))

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
    respx.get(f"{BASE_URL}/rest/api/group/confluence-users/member").mock(return_value=httpx.Response(200, json=fixture))

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
    # Server/DC wraps page results under "page" key
    fixture = {
        "page": {
            "results": [
                {"id": "1", "title": "Root Page", "type": "page", "ancestors": []},
                {
                    "id": "2",
                    "title": "Child Page",
                    "type": "page",
                    "ancestors": [{"id": "1", "title": "Root Page", "type": "page"}],
                },
            ],
            "size": 2,
            "_links": {},
        },
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/space/TESTSPACE/content").mock(return_value=httpx.Response(200, json=fixture))

    result = client.get_space_tree("TESTSPACE")

    assert isinstance(result, SpaceTreeResult)
    assert result.space_key == "TESTSPACE"
    assert result.total_pages == 2
    assert len(result.pages) == 2


# ---------------------------------------------------------------------------
# download_attachment
# ---------------------------------------------------------------------------


@respx.mock
def test_download_attachment(client: ConfluenceClient, tmp_path: Path) -> None:
    content = b"file-content-bytes"
    dl_path = "/download/attachments/99/test.bin?api=v2"
    respx.get(f"{BASE_URL}/rest/api/content/att100").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "att100",
                "title": "test.bin",
                "_links": {"download": dl_path},
            },
        )
    )
    respx.get(f"{BASE_URL}/download/attachments/99/test.bin").mock(return_value=httpx.Response(200, content=content))

    out = client.download_attachment("att100", tmp_path / "test.bin")

    assert out.exists()
    assert out.read_bytes() == content


@respx.mock
def test_download_attachment_publishes_same_directory_part_file(
    client: ConfluenceClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"complete-image-content"
    download_link = "/download/attachments/99/image.png?api=v2"
    respx.get(f"{BASE_URL}/download/attachments/99/image.png").mock(return_value=httpx.Response(200, content=content))
    destination = tmp_path / "image.png"
    real_promote = DirectoryCapability.promote_no_replace
    promote_calls: list[tuple[Path, Path]] = []

    def observe_promote(capability: DirectoryCapability, source_leaf: str, target_leaf: str) -> None:
        source = capability.path_for_leaf(source_leaf)
        target = capability.path_for_leaf(target_leaf)
        promote_calls.append((source, target))
        assert source.parent == destination.parent
        assert source.name.startswith(".atls-download-")
        assert source.name.endswith(".part")
        token = source.name.removeprefix(".atls-download-").removesuffix(".part")
        assert len(token) == 32
        int(token, 16)
        assert ".png" not in source.name
        assert target == destination
        assert not destination.exists()
        assert source.read_bytes() == content
        real_promote(capability, source_leaf, target_leaf)

    monkeypatch.setattr(DirectoryCapability, "promote_no_replace", observe_promote)

    writer = AttachmentWriter(AttachmentWriterKind.NATIVE, tmp_path)
    out = client.download_attachment("att100", destination, download_link=download_link, writer=writer)

    assert len(promote_calls) == 1
    assert out == destination
    assert out.read_bytes() == content
    assert list(tmp_path.glob("*.part")) == []


@respx.mock
def test_download_attachment_retries_part_name_collision_without_deleting_existing_file(
    client: ConfluenceClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"new-content"
    download_link = "/download/attachments/99/report.pdf?api=v2"
    respx.get(f"{BASE_URL}/download/attachments/99/report.pdf").mock(return_value=httpx.Response(200, content=content))
    tokens = iter(["a" * 32, "b" * 32])

    def next_token(_nbytes: int) -> str:
        return next(tokens)

    monkeypatch.setattr("atlassian_skills.core.attachment_io.secrets.token_hex", next_token)
    collision = tmp_path / f".atls-download-{'a' * 32}.part"
    collision.write_bytes(b"owned-by-another-process")
    destination = tmp_path / "report.pdf"

    writer = AttachmentWriter(AttachmentWriterKind.NATIVE, tmp_path)
    out = client.download_attachment("att101", destination, download_link=download_link, writer=writer)

    assert out.read_bytes() == content
    assert collision.read_bytes() == b"owned-by-another-process"
    assert sorted(path.name for path in tmp_path.glob("*.part")) == [collision.name]


@respx.mock
def test_download_attachment_uses_unique_part_names(
    client: ConfluenceClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    download_link = "/download/attachments/99/repeated.bin?api=v2"
    respx.get(f"{BASE_URL}/download/attachments/99/repeated.bin").mock(
        return_value=httpx.Response(200, content=b"content")
    )
    destination = tmp_path / "repeated.bin"
    real_promote = DirectoryCapability.promote_no_replace
    part_source_names: list[str] = []

    def capture_source_name(capability: DirectoryCapability, source_leaf: str, target_leaf: str) -> None:
        source = capability.path_for_leaf(source_leaf)
        if source.name.endswith(".part"):
            part_source_names.append(source.name)
        real_promote(capability, source_leaf, target_leaf)

    monkeypatch.setattr(DirectoryCapability, "promote_no_replace", capture_source_name)

    writer = AttachmentWriter(AttachmentWriterKind.NATIVE, tmp_path)
    client.download_attachment("att102", destination, download_link=download_link, writer=writer)
    client.download_attachment("att102", destination, download_link=download_link, writer=writer)

    assert len(part_source_names) == 2
    assert len(set(part_source_names)) == 2
    assert list(tmp_path.glob("*.part")) == []


@respx.mock
def test_download_attachment_replace_failure_preserves_destination_and_cleans_part_file(
    client: ConfluenceClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    download_link = "/download/attachments/99/existing.bin?api=v2"
    respx.get(f"{BASE_URL}/download/attachments/99/existing.bin").mock(
        return_value=httpx.Response(200, content=b"replacement-content")
    )
    destination = tmp_path / "existing.bin"
    destination.write_bytes(b"original-content")
    replace_error = OSError("replace failed")
    temporary_paths: list[Path] = []

    real_promote = DirectoryCapability.promote_no_replace

    def fail_promote(capability: DirectoryCapability, source_leaf: str, target_leaf: str) -> None:
        source = capability.path_for_leaf(source_leaf)
        target = capability.path_for_leaf(target_leaf)
        if target == destination and source.name.startswith(".atls-download-"):
            temporary_paths.append(source)
            assert source.read_bytes() == b"replacement-content"
            raise replace_error
        real_promote(capability, source_leaf, target_leaf)

    monkeypatch.setattr(DirectoryCapability, "promote_no_replace", fail_promote)

    with pytest.raises(AtlasError) as exc_info:
        writer = AttachmentWriter(AttachmentWriterKind.NATIVE, tmp_path)
        client.download_attachment("att103", destination, download_link=download_link, writer=writer)

    assert str(destination) in str(exc_info.value)
    assert exc_info.value.__cause__ is replace_error
    assert destination.read_bytes() == b"original-content"
    assert temporary_paths
    assert all(not path.exists() for path in temporary_paths)
    assert list(tmp_path.glob("*.part")) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
@respx.mock
def test_download_attachment_new_file_mode_matches_write_bytes(client: ConfluenceClient, tmp_path: Path) -> None:
    download_link = "/download/attachments/99/new.bin?api=v2"
    respx.get(f"{BASE_URL}/download/attachments/99/new.bin").mock(return_value=httpx.Response(200, content=b"content"))
    control = tmp_path / "control.bin"
    control.write_bytes(b"content")
    destination = tmp_path / "new.bin"

    client.download_attachment("att104", destination, download_link=download_link)

    assert stat.S_IMODE(destination.stat().st_mode) == stat.S_IMODE(control.stat().st_mode)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
@respx.mock
def test_download_attachment_preserves_existing_file_mode(client: ConfluenceClient, tmp_path: Path) -> None:
    download_link = "/download/attachments/99/existing-mode.bin?api=v2"
    respx.get(f"{BASE_URL}/download/attachments/99/existing-mode.bin").mock(
        return_value=httpx.Response(200, content=b"replacement-content")
    )
    destination = tmp_path / "existing-mode.bin"
    destination.write_bytes(b"original-content")
    destination.chmod(0o640)

    client.download_attachment("att105", destination, download_link=download_link)

    assert destination.read_bytes() == b"replacement-content"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o640


# ---------------------------------------------------------------------------
# download_all_attachments
# ---------------------------------------------------------------------------


@respx.mock
def test_download_all_attachments(
    client: ConfluenceClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mock list attachments
    list_fixture = {
        "results": [
            {
                "id": "att1",
                "title": "file1.txt",
                "mediaType": "text/plain",
                "fileSize": 100,
                "_links": {"download": "/download/attachments/100/file1.txt?api=v2"},
            },
            {
                "id": "att2",
                "title": "file2.txt",
                "mediaType": "text/plain",
                "fileSize": 200,
                "_links": {"download": "/download/attachments/100/file2.txt?api=v2"},
            },
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(
        return_value=httpx.Response(200, json=list_fixture)
    )
    respx.get(f"{BASE_URL}/download/attachments/100/file1.txt").mock(
        return_value=httpx.Response(200, content=b"content1")
    )
    respx.get(f"{BASE_URL}/download/attachments/100/file2.txt").mock(
        return_value=httpx.Response(200, content=b"content2")
    )
    writer = AttachmentWriter(AttachmentWriterKind.NATIVE, tmp_path.resolve())
    resolve_writer = MagicMock(return_value=writer)
    monkeypatch.setattr("atlassian_skills.confluence.client.resolve_attachment_writer", resolve_writer)

    paths = client.download_all_attachments("100", tmp_path)

    resolve_writer.assert_called_once_with(tmp_path)
    assert len(paths) == 2
    assert (tmp_path / "file1.txt").read_bytes() == b"content1"
    assert (tmp_path / "file2.txt").read_bytes() == b"content2"


# ---------------------------------------------------------------------------
# get_page_history
# ---------------------------------------------------------------------------


@respx.mock
def test_get_page_history_returns_page(client: ConfluenceClient) -> None:
    fixture = _load("get-page-history-v1.json")
    route = respx.get(f"{BASE_URL}/rest/api/content/12345678").mock(return_value=httpx.Response(200, json=fixture))

    page = client.get_page_history("12345678", version=1)

    req = route.calls[0].request
    assert req.url.params["status"] == "historical"
    assert req.url.params["version"] == "1"
    assert isinstance(page, Page)
    assert page.id == "12345678"


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
    route = respx.get(f"{BASE_URL}/rest/api/content/100").mock(return_value=httpx.Response(200, json=page_data))

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
    respx.get(f"{BASE_URL}/rest/api/content/200/child/attachment").mock(return_value=httpx.Response(200, json=fixture))

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
    respx.get(f"{BASE_URL}/rest/api/content/300/child/page").mock(return_value=httpx.Response(200, json=fixture))

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
    fixture = _load("get-space-tree-sample.json")
    # Server/DC wraps page results under "page" key
    api_response = {
        "page": {
            "results": fixture["pages"],
            "size": len(fixture["pages"]),
            "_links": {},
        },
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/space/TESTSPACE/content").mock(return_value=httpx.Response(200, json=api_response))

    result = client.get_space_tree("TESTSPACE")

    assert isinstance(result, SpaceTreeResult)
    assert result.space_key == "TESTSPACE"
    assert result.total_pages == len(fixture["pages"])
    assert len(result.pages) == len(fixture["pages"])
    assert result.pages[0].title == "01. [샘플 모듈] Service Architecture"


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
            {
                "id": "att22",
                "title": "data.xlsx",
                "mediaType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "fileSize": 8192,
            },
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/content/500/child/attachment").mock(return_value=httpx.Response(200, json=fixture))

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
    dl_path = "/download/attachments/99/output.bin?api=v2"
    respx.get(f"{BASE_URL}/rest/api/content/att500").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "att500",
                "title": "output.bin",
                "_links": {"download": dl_path},
            },
        )
    )
    respx.get(f"{BASE_URL}/download/attachments/99/output.bin").mock(return_value=httpx.Response(200, content=content))

    out = client.download_attachment("att500", tmp_path / "output.bin")

    assert out == tmp_path / "output.bin"
    assert out.read_bytes() == content


@respx.mock
def test_download_attachment_creates_parent_dirs(client: ConfluenceClient, tmp_path: Path) -> None:
    content = b"file content"
    dl_path = "/download/attachments/99/file.txt?api=v2"
    respx.get(f"{BASE_URL}/rest/api/content/att600").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "att600",
                "title": "file.txt",
                "_links": {"download": dl_path},
            },
        )
    )
    respx.get(f"{BASE_URL}/download/attachments/99/file.txt").mock(return_value=httpx.Response(200, content=content))

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
    respx.get(f"{BASE_URL}/rest/api/group/confluence-users/member").mock(return_value=httpx.Response(200, json=fixture))

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
    respx.get(f"{BASE_URL}/rest/api/group/confluence-users/member").mock(return_value=httpx.Response(200, json=fixture))

    users = client.search_users("bob@corp.com")

    assert len(users) == 1
    assert users[0].name == "bob.lee"
