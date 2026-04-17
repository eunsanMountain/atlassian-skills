from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from atlassian_skills.confluence.client import ConfluenceClient
from atlassian_skills.core.auth import Credential

FIXTURES = Path(__file__).parent.parent / "fixtures" / "confluence"
BASE_URL = "https://confluence.example.com"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def cred() -> Credential:
    return Credential(method="pat", token="test-token")


@pytest.fixture
def client(cred: Credential) -> ConfluenceClient:
    return ConfluenceClient(BASE_URL, cred)


# ---------------------------------------------------------------------------
# create_page
# ---------------------------------------------------------------------------


@respx.mock
def test_create_page(client: ConfluenceClient) -> None:
    expected = _load("create-page-expected.json")
    respx.post(f"{BASE_URL}/rest/api/content").mock(return_value=httpx.Response(200, json=expected))

    result = client.create_page("TEST", "Test Page", "<p>Test content</p>")

    assert result["id"] == "999999999"
    assert result["title"] == "Test Page"
    assert result["space"]["key"] == "TEST"


@respx.mock
def test_create_page_with_ancestor(client: ConfluenceClient) -> None:
    expected = _load("create-page-expected.json")
    route = respx.post(f"{BASE_URL}/rest/api/content").mock(return_value=httpx.Response(200, json=expected))

    result = client.create_page("TEST", "Test Page", "<p>body</p>", ancestor_id="12345")

    assert result["id"] == "999999999"
    # Verify the request body included ancestors
    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body["ancestors"] == [{"id": "12345"}]


# ---------------------------------------------------------------------------
# update_page
# ---------------------------------------------------------------------------


@respx.mock
def test_update_page(client: ConfluenceClient) -> None:
    expected = _load("update-page-expected.json")
    route = respx.put(f"{BASE_URL}/rest/api/content/429140627").mock(return_value=httpx.Response(200, json=expected))

    result = client.update_page("429140627", "Updated Title", "<p>Updated content</p>", version_number=3)

    assert result["id"] == "429140627"
    assert result["version"]["number"] == 3
    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body["version"]["number"] == 3
    assert sent_body["title"] == "Updated Title"


# ---------------------------------------------------------------------------
# delete_page
# ---------------------------------------------------------------------------


@respx.mock
def test_delete_page(client: ConfluenceClient) -> None:
    respx.delete(f"{BASE_URL}/rest/api/content/100").mock(return_value=httpx.Response(204))

    # Should not raise
    result = client.delete_page("100")
    assert result is None


# ---------------------------------------------------------------------------
# move_page
# ---------------------------------------------------------------------------


@respx.mock
def test_move_page(client: ConfluenceClient) -> None:
    expected = {"id": "100", "title": "Moved"}
    respx.post(f"{BASE_URL}/rest/api/content/100/move/append/target/200").mock(
        return_value=httpx.Response(200, json=expected)
    )

    result = client.move_page("100", "append", "200")

    assert result["id"] == "100"


# ---------------------------------------------------------------------------
# add_comment
# ---------------------------------------------------------------------------


@respx.mock
def test_add_comment(client: ConfluenceClient) -> None:
    expected = {"id": "600", "type": "comment", "title": "Re: Page"}
    route = respx.post(f"{BASE_URL}/rest/api/content").mock(return_value=httpx.Response(200, json=expected))

    result = client.add_comment("100", "<p>My comment</p>")

    assert result["id"] == "600"
    assert result["type"] == "comment"
    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body["type"] == "comment"
    assert sent_body["container"] == {"id": "100", "type": "page"}
    assert sent_body["body"]["storage"]["value"] == "<p>My comment</p>"


# ---------------------------------------------------------------------------
# reply_to_comment
# ---------------------------------------------------------------------------


@respx.mock
def test_reply_to_comment(client: ConfluenceClient) -> None:
    parent = {"id": "600", "type": "comment", "container": {"id": "100", "type": "page"}}
    respx.get(f"{BASE_URL}/rest/api/content/600?expand=container").mock(return_value=httpx.Response(200, json=parent))
    expected = {"id": "601", "type": "comment", "title": "Re: Re: Page"}
    respx.post(f"{BASE_URL}/rest/api/content").mock(return_value=httpx.Response(200, json=expected))

    result = client.reply_to_comment("600", "<p>Reply</p>")

    assert result["id"] == "601"


# ---------------------------------------------------------------------------
# add_label
# ---------------------------------------------------------------------------


@respx.mock
def test_add_label(client: ConfluenceClient) -> None:
    expected = {"results": [{"name": "important", "prefix": "global"}, {"name": "v2", "prefix": "global"}]}
    route = respx.post(f"{BASE_URL}/rest/api/content/100/label").mock(return_value=httpx.Response(200, json=expected))

    result = client.add_label("100", ["important", "v2"])

    assert "results" in result
    sent_body = json.loads(route.calls[0].request.content)
    assert len(sent_body) == 2
    assert sent_body[0]["name"] == "important"
    assert sent_body[0]["prefix"] == "global"


# ---------------------------------------------------------------------------
# upload_attachment
# ---------------------------------------------------------------------------


@respx.mock
def test_upload_attachment(client: ConfluenceClient, tmp_path: Path) -> None:
    test_file = tmp_path / "test.txt"
    test_file.write_text("hello world")

    expected = {
        "results": [{"id": "att200", "title": "test.txt", "mediaType": "text/plain"}],
    }
    respx.post(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(
        return_value=httpx.Response(200, json=expected)
    )

    result = client.upload_attachment("100", test_file)

    assert result["results"][0]["id"] == "att200"


# ---------------------------------------------------------------------------
# upload_attachments_batch (skip mode)
# ---------------------------------------------------------------------------


@respx.mock
def test_upload_attachments_batch_skip(client: ConfluenceClient, tmp_path: Path) -> None:
    existing_file = tmp_path / "existing.txt"
    existing_file.write_text("old")
    new_file = tmp_path / "new.txt"
    new_file.write_text("new content")

    # Mock list_attachments to return existing.txt
    list_fixture = {
        "results": [
            {"id": "att1", "title": "existing.txt", "mediaType": "text/plain", "fileSize": 3},
        ],
        "_links": {},
    }
    respx.get(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(
        return_value=httpx.Response(200, json=list_fixture)
    )

    upload_result = {"results": [{"id": "att2", "title": "new.txt"}]}
    respx.post(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(
        return_value=httpx.Response(200, json=upload_result)
    )

    results = client.upload_attachments_batch("100", [existing_file, new_file], if_exists="skip")

    assert len(results) == 2
    assert results[0]["skipped"] is True
    assert results[0]["title"] == "existing.txt"
    assert results[1]["results"][0]["title"] == "new.txt"


# ---------------------------------------------------------------------------
# delete_attachment
# ---------------------------------------------------------------------------


@respx.mock
def test_delete_attachment(client: ConfluenceClient) -> None:
    respx.delete(f"{BASE_URL}/rest/api/content/att100").mock(return_value=httpx.Response(204))

    result = client.delete_attachment("att100")
    assert result is None


# ---------------------------------------------------------------------------
# move_page — verifies URL structure (position + target)
# ---------------------------------------------------------------------------


@respx.mock
def test_move_page_above(client: ConfluenceClient) -> None:
    expected = {"id": "100", "title": "Moved Above"}
    route = respx.post(f"{BASE_URL}/rest/api/content/100/move/above/target/300").mock(
        return_value=httpx.Response(200, json=expected)
    )

    result = client.move_page("100", "above", "300")

    assert route.called
    assert result["id"] == "100"


@respx.mock
def test_move_page_below(client: ConfluenceClient) -> None:
    expected = {"id": "200", "title": "Moved Below"}
    respx.post(f"{BASE_URL}/rest/api/content/200/move/below/target/400").mock(
        return_value=httpx.Response(200, json=expected)
    )

    result = client.move_page("200", "below", "400")

    assert result["id"] == "200"


# ---------------------------------------------------------------------------
# reply_to_comment — verifies child comment endpoint
# ---------------------------------------------------------------------------


@respx.mock
def test_reply_to_comment_uses_content_endpoint(client: ConfluenceClient) -> None:
    parent = {"id": "600", "type": "comment", "container": {"id": "100", "type": "page"}}
    respx.get(f"{BASE_URL}/rest/api/content/600?expand=container").mock(return_value=httpx.Response(200, json=parent))
    expected = {"id": "700", "type": "comment"}
    route = respx.post(f"{BASE_URL}/rest/api/content").mock(return_value=httpx.Response(200, json=expected))

    result = client.reply_to_comment("600", "<p>My reply</p>")

    assert route.called
    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["type"] == "comment"
    assert body["container"] == {"id": "100", "type": "page"}
    assert body["ancestors"] == [{"id": "600"}]
    assert body["body"]["storage"]["value"] == "<p>My reply</p>"
    assert result["id"] == "700"


@respx.mock
def test_reply_to_comment_returns_new_comment_id(client: ConfluenceClient) -> None:
    parent = {"id": "800", "type": "comment", "container": {"id": "200", "type": "page"}}
    respx.get(f"{BASE_URL}/rest/api/content/800?expand=container").mock(return_value=httpx.Response(200, json=parent))
    expected = {"id": "801", "type": "comment", "title": "Re: Discussion"}
    respx.post(f"{BASE_URL}/rest/api/content").mock(return_value=httpx.Response(200, json=expected))

    result = client.reply_to_comment("800", "<p>Another reply</p>")

    assert result["id"] == "801"
    assert result["type"] == "comment"


# ---------------------------------------------------------------------------
# add_label — verifies payload shape (list of {name, prefix})
# ---------------------------------------------------------------------------


@respx.mock
def test_add_label_single(client: ConfluenceClient) -> None:
    expected = {"results": [{"name": "release", "prefix": "global"}]}
    route = respx.post(f"{BASE_URL}/rest/api/content/100/label").mock(return_value=httpx.Response(200, json=expected))

    result = client.add_label("100", ["release"])

    req = route.calls[0].request
    body = json.loads(req.content)
    assert len(body) == 1
    assert body[0]["name"] == "release"
    assert body[0]["prefix"] == "global"
    assert "results" in result


@respx.mock
def test_add_label_multiple_labels(client: ConfluenceClient) -> None:
    expected = {"results": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}
    route = respx.post(f"{BASE_URL}/rest/api/content/200/label").mock(return_value=httpx.Response(200, json=expected))

    client.add_label("200", ["a", "b", "c"])

    req = route.calls[0].request
    body = json.loads(req.content)
    assert len(body) == 3
    names = [item["name"] for item in body]
    assert names == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# upload_attachment — with comment
# ---------------------------------------------------------------------------


@respx.mock
def test_upload_attachment_with_comment(client: ConfluenceClient, tmp_path: Path) -> None:
    test_file = tmp_path / "report.pdf"
    test_file.write_bytes(b"%PDF-1.4 fake content")

    expected = {"results": [{"id": "att300", "title": "report.pdf", "mediaType": "application/pdf"}]}
    respx.post(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(
        return_value=httpx.Response(200, json=expected)
    )

    result = client.upload_attachment("100", test_file, comment="Quarterly report")

    assert result["results"][0]["id"] == "att300"


@respx.mock
def test_upload_attachment_sets_nocheck_header(client: ConfluenceClient, tmp_path: Path) -> None:
    test_file = tmp_path / "img.png"
    test_file.write_bytes(b"\x89PNG\r\n")

    expected = {"results": [{"id": "att400", "title": "img.png"}]}
    route = respx.post(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(
        return_value=httpx.Response(200, json=expected)
    )

    client.upload_attachment("100", test_file)

    req = route.calls[0].request
    assert req.headers.get("X-Atlassian-Token") == "nocheck"


# ---------------------------------------------------------------------------
# upload_attachments_batch — replace mode skips existing check
# ---------------------------------------------------------------------------


@respx.mock
def test_upload_attachments_batch_replace_mode(client: ConfluenceClient, tmp_path: Path) -> None:
    file_a = tmp_path / "fileA.txt"
    file_a.write_text("content A")

    upload_result = {"results": [{"id": "att500", "title": "fileA.txt"}]}
    route = respx.post(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(
        return_value=httpx.Response(200, json=upload_result)
    )

    # With if_exists="replace", no GET to list attachments should be made
    results = client.upload_attachments_batch("100", [file_a], if_exists="replace")

    assert len(results) == 1
    assert results[0]["results"][0]["id"] == "att500"
    # No list_attachments call means no GET was made
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# delete_attachment — verifies DELETE endpoint
# ---------------------------------------------------------------------------


@respx.mock
def test_delete_attachment_calls_correct_endpoint(client: ConfluenceClient) -> None:
    route = respx.delete(f"{BASE_URL}/rest/api/content/att999").mock(return_value=httpx.Response(204))

    result = client.delete_attachment("att999")

    assert route.called
    assert result is None


# ---------------------------------------------------------------------------
# delete_page — verifies DELETE endpoint and returns None
# ---------------------------------------------------------------------------


@respx.mock
def test_delete_page_calls_correct_endpoint(client: ConfluenceClient) -> None:
    route = respx.delete(f"{BASE_URL}/rest/api/content/555").mock(return_value=httpx.Response(204))

    result = client.delete_page("555")

    assert route.called
    assert result is None


@respx.mock
def test_delete_page_404_raises(client: ConfluenceClient) -> None:
    from atlassian_skills.core.errors import NotFoundError

    respx.delete(f"{BASE_URL}/rest/api/content/9999").mock(return_value=httpx.Response(404, text="Page not found"))

    with pytest.raises(NotFoundError):
        client.delete_page("9999")
