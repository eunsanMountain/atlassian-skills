from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from atlassian_skills.confluence.client import ConfluenceClient, _safe_filename
from atlassian_skills.confluence.models import (
    Attachment,
    ConfluenceSearchResult,
    Label,
    Page,
    SpaceTreeResult,
)
from atlassian_skills.core.auth import Credential
from atlassian_skills.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError

BASE_URL = "https://wiki.example.com"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cred() -> Credential:
    return Credential(method="pat", token="test-token")


@pytest.fixture
def client(cred: Credential) -> ConfluenceClient:
    return ConfluenceClient(BASE_URL, cred)


# ===========================================================================
# 1. Error responses
# ===========================================================================


@respx.mock
def test_get_page_404_raises_not_found(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/content/999").mock(
        return_value=httpx.Response(404, json={"message": "Page not found"})
    )

    with pytest.raises(NotFoundError) as exc_info:
        client.get_page("999")

    assert exc_info.value.http_status == 404


@respx.mock
def test_get_page_403_raises_forbidden(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/content/100").mock(
        return_value=httpx.Response(403, json={"message": "Access denied"})
    )

    with pytest.raises(ForbiddenError) as exc_info:
        client.get_page("100")

    assert exc_info.value.http_status == 403


@respx.mock
def test_search_400_raises_validation_error(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/search").mock(return_value=httpx.Response(400, json={"message": "Invalid CQL"}))

    with pytest.raises(ValidationError) as exc_info:
        client.search("INVALID CQL !!!!")

    assert exc_info.value.http_status == 400


@respx.mock
def test_create_page_409_raises_conflict(client: ConfluenceClient) -> None:
    respx.post(f"{BASE_URL}/rest/api/content").mock(
        return_value=httpx.Response(409, json={"message": "A page with this title already exists"})
    )

    with pytest.raises(ConflictError) as exc_info:
        client.create_page("TEST", "Duplicate Title", "<p>body</p>")

    assert exc_info.value.http_status == 409
    assert exc_info.value.exit_code == 4


@respx.mock
def test_update_page_409_stale_version_raises_conflict(client: ConfluenceClient) -> None:
    respx.put(f"{BASE_URL}/rest/api/content/100").mock(
        return_value=httpx.Response(409, json={"message": "Version conflict"})
    )

    with pytest.raises(ConflictError) as exc_info:
        client.update_page("100", "Title", "<p>body</p>", version_number=1)

    assert exc_info.value.http_status == 409
    assert "if-version" in (exc_info.value.hint or "").lower()


@respx.mock
def test_delete_page_404_raises_not_found(client: ConfluenceClient) -> None:
    respx.delete(f"{BASE_URL}/rest/api/content/999").mock(
        return_value=httpx.Response(404, json={"message": "Page not found"})
    )

    with pytest.raises(NotFoundError):
        client.delete_page("999")


# ===========================================================================
# 2. get_page edge cases
# ===========================================================================


@respx.mock
def test_get_page_include_body_false_omits_body_expand(client: ConfluenceClient) -> None:
    fixture = {
        "id": "100",
        "title": "Compact Page",
        "type": "page",
        "version": {"number": 1},
        "space": {"key": "TEST", "name": "Test Space"},
    }
    route = respx.get(f"{BASE_URL}/rest/api/content/100").mock(return_value=httpx.Response(200, json=fixture))

    page = client.get_page("100", include_body=False)

    assert isinstance(page, Page)
    # Verify expand param does NOT include body.storage
    sent_params = route.calls[0].request.url.params
    expand = sent_params.get("expand", "")
    assert "body" not in expand


@respx.mock
def test_get_page_empty_body(client: ConfluenceClient) -> None:
    fixture = {
        "id": "100",
        "title": "Empty Body Page",
        "type": "page",
        "version": {"number": 1},
        "space": {"key": "TEST", "name": "Test Space"},
        "body": {"storage": {"value": "", "representation": "storage"}},
    }
    respx.get(f"{BASE_URL}/rest/api/content/100").mock(return_value=httpx.Response(200, json=fixture))

    page = client.get_page("100")

    assert isinstance(page, Page)
    assert page.body_storage == ""


@respx.mock
def test_get_page_no_space_info(client: ConfluenceClient) -> None:
    fixture = {
        "id": "100",
        "title": "No Space Page",
        "type": "page",
        "version": {"number": 1},
        # space field absent
        "body": {"storage": {"value": "<p>content</p>", "representation": "storage"}},
    }
    respx.get(f"{BASE_URL}/rest/api/content/100").mock(return_value=httpx.Response(200, json=fixture))

    page = client.get_page("100")

    assert isinstance(page, Page)
    assert page.space is None


# ===========================================================================
# 3. get_page_diff edge cases
# ===========================================================================


@respx.mock
def test_get_page_diff_same_version_returns_empty(client: ConfluenceClient) -> None:
    fixture = {
        "id": "100",
        "title": "Test Page",
        "type": "page",
        "body": {"storage": {"value": "same content", "representation": "storage"}},
        "version": {"number": 3},
    }
    route = respx.get(f"{BASE_URL}/rest/api/content/100")
    route.side_effect = [
        httpx.Response(200, json=fixture),
        httpx.Response(200, json=fixture),
    ]

    diff = client.get_page_diff("100", 3, 3)

    assert diff == ""


@respx.mock
def test_get_page_diff_identical_content_returns_empty(client: ConfluenceClient) -> None:
    content = "<p>identical content line1</p>\n<p>line2</p>"
    v1 = {
        "id": "100",
        "title": "Test Page",
        "type": "page",
        "body": {"storage": {"value": content, "representation": "storage"}},
        "version": {"number": 1},
    }
    v2 = {
        "id": "100",
        "title": "Test Page",
        "type": "page",
        "body": {"storage": {"value": content, "representation": "storage"}},
        "version": {"number": 2},
    }
    route = respx.get(f"{BASE_URL}/rest/api/content/100")
    route.side_effect = [
        httpx.Response(200, json=v1),
        httpx.Response(200, json=v2),
    ]

    diff = client.get_page_diff("100", 1, 2)

    assert diff == ""


# ===========================================================================
# 4. get_page_images edge cases
# ===========================================================================


@respx.mock
def test_get_page_images_no_attachments_returns_empty(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(
        return_value=httpx.Response(200, json={"results": [], "_links": {}})
    )

    images = client.get_page_images("100")

    assert images == []


@respx.mock
def test_get_page_images_mixed_returns_only_images(client: ConfluenceClient) -> None:
    fixture = {
        "results": [
            {"id": "att1", "title": "photo.jpg", "mediaType": "image/jpeg", "fileSize": 1000},
            {"id": "att2", "title": "report.pdf", "mediaType": "application/pdf", "fileSize": 5000},
            {"id": "att3", "title": "data.xlsx", "mediaType": "application/vnd.ms-excel", "fileSize": 2000},
            {"id": "att4", "title": "logo.png", "mediaType": "image/png", "fileSize": 800},
            {"id": "att5", "title": "sketch.svg", "mediaType": "image/svg+xml", "fileSize": 300},
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(return_value=httpx.Response(200, json=fixture))

    images = client.get_page_images("100")

    assert len(images) == 3
    titles = {a.title for a in images}
    assert titles == {"photo.jpg", "logo.png", "sketch.svg"}
    assert all(isinstance(a, Attachment) for a in images)
    assert all((a.media_type or "").startswith("image/") for a in images)


# ===========================================================================
# 5. search edge cases
# ===========================================================================


@respx.mock
def test_search_empty_results(client: ConfluenceClient) -> None:
    fixture = {
        "results": [],
        "start": 0,
        "limit": 25,
        "size": 0,
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/search").mock(return_value=httpx.Response(200, json=fixture))

    result = client.search("type=page AND title='NonExistent'")

    assert isinstance(result, ConfluenceSearchResult)
    assert result.results == []
    assert result.total == 0


@respx.mock
def test_search_cql_special_characters(client: ConfluenceClient) -> None:
    fixture = {
        "results": [{"id": "42", "title": "C++ Guide", "type": "page"}],
        "start": 0,
        "limit": 25,
        "size": 1,
        "_links": {},
    }
    route = respx.get(f"{BASE_URL}/rest/api/search").mock(return_value=httpx.Response(200, json=fixture))

    cql = 'text~"C++ & std::vector" AND space.key="DEV"'
    result = client.search(cql)

    assert len(result.results) == 1
    sent_params = route.calls[0].request.url.params
    assert "cql" in sent_params


@respx.mock
def test_search_with_limit_parameter(client: ConfluenceClient) -> None:
    fixture = {
        "results": [{"id": str(i), "title": f"Page {i}", "type": "page"} for i in range(5)],
        "start": 0,
        "limit": 5,
        "size": 5,
        "_links": {},
    }
    route = respx.get(f"{BASE_URL}/rest/api/search").mock(return_value=httpx.Response(200, json=fixture))

    result = client.search("type=page", limit=5)

    assert len(result.results) == 5
    assert result.limit == 5
    sent_params = route.calls[0].request.url.params
    assert sent_params.get("limit") == "5"


# ===========================================================================
# 6. get_children edge cases
# ===========================================================================


@respx.mock
def test_get_children_empty_returns_empty_list(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/content/100/child/page").mock(
        return_value=httpx.Response(200, json={"results": [], "_links": {}})
    )

    children = client.get_children("100")

    assert children == []


# ===========================================================================
# 7. get_space_tree edge cases
# ===========================================================================


@respx.mock
def test_get_space_tree_empty_space(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/space/EMPTY/content").mock(
        return_value=httpx.Response(200, json={"page": {"results": [], "size": 0, "_links": {}}, "_links": {}})
    )

    result = client.get_space_tree("EMPTY")

    assert isinstance(result, SpaceTreeResult)
    assert result.space_key == "EMPTY"
    assert result.total_pages == 0
    assert result.pages == []


# ===========================================================================
# 8. Attachment operations
# ===========================================================================


@respx.mock
def test_download_attachment_to_specified_path(client: ConfluenceClient, tmp_path: Path) -> None:
    content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    dl_path = "/download/attachments/123/image.png?version=1&api=v2"
    # When no download_link is given, client fetches metadata to find the link
    respx.get(f"{BASE_URL}/rest/api/content/att42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "att42",
                "title": "image.png",
                "_links": {"download": dl_path},
            },
        )
    )
    respx.get(f"{BASE_URL}/download/attachments/123/image.png").mock(return_value=httpx.Response(200, content=content))
    dest = tmp_path / "subdir" / "image.png"

    out = client.download_attachment("att42", dest)

    assert out == dest
    assert out.exists()
    assert out.read_bytes() == content


@respx.mock
def test_download_all_attachments_mixed_content_types(client: ConfluenceClient, tmp_path: Path) -> None:
    list_fixture = {
        "results": [
            {
                "id": "att1",
                "title": "photo.jpg",
                "mediaType": "image/jpeg",
                "fileSize": 1000,
                "_links": {"download": "/download/attachments/200/photo.jpg?api=v2"},
            },
            {
                "id": "att2",
                "title": "notes.pdf",
                "mediaType": "application/pdf",
                "fileSize": 5000,
                "_links": {"download": "/download/attachments/200/notes.pdf?api=v2"},
            },
            {
                "id": "att3",
                "title": "data.csv",
                "mediaType": "text/csv",
                "fileSize": 200,
                "_links": {"download": "/download/attachments/200/data.csv?api=v2"},
            },
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/content/200/child/attachment").mock(
        return_value=httpx.Response(200, json=list_fixture)
    )
    respx.get(f"{BASE_URL}/download/attachments/200/photo.jpg").mock(
        return_value=httpx.Response(200, content=b"jpg-data")
    )
    respx.get(f"{BASE_URL}/download/attachments/200/notes.pdf").mock(
        return_value=httpx.Response(200, content=b"pdf-data")
    )
    respx.get(f"{BASE_URL}/download/attachments/200/data.csv").mock(
        return_value=httpx.Response(200, content=b"csv-data")
    )

    paths = client.download_all_attachments("200", tmp_path)

    assert len(paths) == 3
    assert (tmp_path / "photo.jpg").read_bytes() == b"jpg-data"
    assert (tmp_path / "notes.pdf").read_bytes() == b"pdf-data"
    assert (tmp_path / "data.csv").read_bytes() == b"csv-data"


@respx.mock
def test_upload_attachment_success(client: ConfluenceClient, tmp_path: Path) -> None:
    file_path = tmp_path / "document.txt"
    file_path.write_text("hello world")
    expected = {"results": [{"id": "att300", "title": "document.txt", "mediaType": "text/plain"}]}
    respx.post(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(
        return_value=httpx.Response(200, json=expected)
    )

    result = client.upload_attachment("100", file_path)

    assert result["results"][0]["id"] == "att300"
    assert result["results"][0]["title"] == "document.txt"


@respx.mock
def test_upload_attachments_batch_replace_mode(client: ConfluenceClient, tmp_path: Path) -> None:
    file1 = tmp_path / "report.txt"
    file1.write_text("report content")
    file2 = tmp_path / "summary.txt"
    file2.write_text("summary content")

    expected1 = {"results": [{"id": "att10", "title": "report.txt"}]}
    expected2 = {"results": [{"id": "att11", "title": "summary.txt"}]}
    route = respx.post(f"{BASE_URL}/rest/api/content/100/child/attachment")
    route.side_effect = [
        httpx.Response(200, json=expected1),
        httpx.Response(200, json=expected2),
    ]

    results = client.upload_attachments_batch("100", [file1, file2], if_exists="replace")

    assert len(results) == 2
    assert results[0]["results"][0]["title"] == "report.txt"
    assert results[1]["results"][0]["title"] == "summary.txt"


@respx.mock
def test_delete_attachment_404_raises_not_found(client: ConfluenceClient) -> None:
    respx.delete(f"{BASE_URL}/rest/api/content/att999").mock(
        return_value=httpx.Response(404, json={"message": "Attachment not found"})
    )

    with pytest.raises(NotFoundError):
        client.delete_attachment("att999")


# ===========================================================================
# 9. _safe_filename function
# ===========================================================================


def test_safe_filename_path_traversal_basename_only() -> None:
    result = _safe_filename("../../../etc/passwd", "fallback123")
    # os.path.basename strips directory components
    assert result == "passwd"
    assert ".." not in result
    assert "/" not in result


def test_safe_filename_nested_traversal() -> None:
    result = _safe_filename("../../secret.txt", "fallback")
    assert result == "secret.txt"


def test_safe_filename_empty_string_uses_fallback() -> None:
    result = _safe_filename("", "att42")
    assert result == "attachment_att42"


def test_safe_filename_only_dots_uses_fallback() -> None:
    # lstrip(".") on "..." yields "", so fallback is used
    result = _safe_filename("...", "att99")
    assert result == "attachment_att99"


def test_safe_filename_dot_prefix_stripped() -> None:
    result = _safe_filename(".hidden-file", "att1")
    # lstrip(".") removes leading dot(s)
    assert result == "hidden-file"
    assert not result.startswith(".")


def test_safe_filename_normal_filename_unchanged() -> None:
    result = _safe_filename("diagram.png", "att1")
    assert result == "diagram.png"


def test_safe_filename_with_spaces() -> None:
    result = _safe_filename("my document.pdf", "att2")
    assert result == "my document.pdf"


# ===========================================================================
# 10. Comment operations
# ===========================================================================


@respx.mock
def test_add_comment_success(client: ConfluenceClient) -> None:
    expected = {"id": "700", "type": "comment", "title": "Re: My Page"}
    route = respx.post(f"{BASE_URL}/rest/api/content").mock(return_value=httpx.Response(200, json=expected))

    result = client.add_comment("100", "<p>A new comment</p>")

    assert result["id"] == "700"
    assert result["type"] == "comment"
    sent = json.loads(route.calls[0].request.content)
    assert sent["type"] == "comment"
    assert sent["container"] == {"id": "100", "type": "page"}
    assert sent["body"]["storage"]["value"] == "<p>A new comment</p>"
    assert sent["body"]["storage"]["representation"] == "storage"


@respx.mock
def test_reply_to_comment_nested(client: ConfluenceClient) -> None:
    parent = {"id": "700", "type": "comment", "container": {"id": "100", "type": "page"}}
    respx.get(f"{BASE_URL}/rest/api/content/700?expand=container").mock(return_value=httpx.Response(200, json=parent))
    expected = {"id": "701", "type": "comment", "title": "Re: Re: My Page"}
    route = respx.post(f"{BASE_URL}/rest/api/content").mock(return_value=httpx.Response(200, json=expected))

    result = client.reply_to_comment("700", "<p>Nested reply</p>")

    assert result["id"] == "701"
    sent = json.loads(route.calls[0].request.content)
    assert sent["container"] == {"id": "100", "type": "page"}
    assert sent["ancestors"] == [{"id": "700"}]
    assert sent["body"]["storage"]["value"] == "<p>Nested reply</p>"


# ===========================================================================
# 11. Label operations
# ===========================================================================


@respx.mock
def test_add_label_to_page(client: ConfluenceClient) -> None:
    expected = {"results": [{"name": "release", "prefix": "global"}, {"name": "v3", "prefix": "global"}]}
    route = respx.post(f"{BASE_URL}/rest/api/content/100/label").mock(return_value=httpx.Response(200, json=expected))

    result = client.add_label("100", ["release", "v3"])

    assert "results" in result
    sent = json.loads(route.calls[0].request.content)
    assert len(sent) == 2
    names = {item["name"] for item in sent}
    assert names == {"release", "v3"}
    assert all(item["prefix"] == "global" for item in sent)


@respx.mock
def test_list_labels_empty_result(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/content/100/label").mock(return_value=httpx.Response(200, json={"results": []}))

    labels = client.list_labels("100")

    assert labels == []
    assert isinstance(labels, list)


@respx.mock
def test_list_labels_with_results(client: ConfluenceClient) -> None:
    fixture = {
        "results": [
            {"id": "10", "name": "important", "prefix": "global"},
            {"id": "11", "name": "wip", "prefix": "global"},
        ]
    }
    respx.get(f"{BASE_URL}/rest/api/content/100/label").mock(return_value=httpx.Response(200, json=fixture))

    labels = client.list_labels("100")

    assert len(labels) == 2
    assert all(isinstance(lb, Label) for lb in labels)
    assert {lb.name for lb in labels} == {"important", "wip"}


# ===========================================================================
# 12. move_page
# ===========================================================================


@respx.mock
def test_move_page_to_different_parent(client: ConfluenceClient) -> None:
    expected = {"id": "100", "title": "Moved Page"}
    respx.post(f"{BASE_URL}/rest/api/content/100/move/append/target/999").mock(
        return_value=httpx.Response(200, json=expected)
    )

    result = client.move_page("100", "append", "999")

    assert result["id"] == "100"
    assert result["title"] == "Moved Page"


@respx.mock
def test_move_page_above_target(client: ConfluenceClient) -> None:
    expected = {"id": "200", "title": "Sibling Page"}
    respx.post(f"{BASE_URL}/rest/api/content/200/move/above/target/300").mock(
        return_value=httpx.Response(200, json=expected)
    )

    result = client.move_page("200", "above", "300")

    assert result["id"] == "200"


@respx.mock
def test_move_page_below_target(client: ConfluenceClient) -> None:
    expected = {"id": "400", "title": "Below Page"}
    respx.post(f"{BASE_URL}/rest/api/content/400/move/below/target/500").mock(
        return_value=httpx.Response(200, json=expected)
    )

    result = client.move_page("400", "below", "500")

    assert result["id"] == "400"


# ===========================================================================
# 13. search_users edge cases
# ===========================================================================


@respx.mock
def test_search_users_empty_query_returns_all(client: ConfluenceClient) -> None:
    fixture = {
        "results": [
            {"displayName": "Alice Kim", "name": "alice.kim", "emailAddress": "alice@corp.com", "key": "U1"},
            {"displayName": "Bob Lee", "name": "bob.lee", "emailAddress": "bob@corp.com", "key": "U2"},
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/group/confluence-users/member").mock(return_value=httpx.Response(200, json=fixture))

    users = client.search_users("")

    # Empty query returns all users without filtering
    assert len(users) == 2


@respx.mock
def test_search_users_various_fields(client: ConfluenceClient) -> None:
    fixture = {
        "results": [
            {"displayName": "Alice Kim", "name": "alice.kim", "emailAddress": "alice@corp.com", "key": "U1"},
            {"displayName": "Bob Lee", "name": "bob.lee", "emailAddress": "bob@corp.com", "key": "U2"},
            {"displayName": "Carol Park", "name": "carol", "emailAddress": "carol@alice.org", "key": "U3"},
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/group/confluence-users/member").mock(return_value=httpx.Response(200, json=fixture))

    # Match by email domain containing 'alice'
    users = client.search_users("alice")

    # alice.kim (name match) + carol@alice.org (email match)
    assert len(users) == 2
    names = {u.name for u in users}
    assert "alice.kim" in names
    assert "carol" in names


@respx.mock
def test_search_users_no_match_returns_empty(client: ConfluenceClient) -> None:
    fixture = {
        "results": [
            {"displayName": "Alice Kim", "name": "alice.kim", "emailAddress": "alice@corp.com", "key": "U1"},
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/group/confluence-users/member").mock(return_value=httpx.Response(200, json=fixture))

    users = client.search_users("zzznomatch")

    assert users == []
