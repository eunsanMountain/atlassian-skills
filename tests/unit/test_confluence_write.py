from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from atlassian_skills.confluence.client import ConfluenceClient
from atlassian_skills.core.auth import Credential
from atlassian_skills.core.errors import NetworkError, ValidationError

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


@respx.mock
def test_create_page_does_not_retry_an_ambiguous_post(client: ConfluenceClient) -> None:
    route = respx.post(f"{BASE_URL}/rest/api/content").mock(
        side_effect=[
            httpx.ConnectError("synthetic response loss"),
            httpx.Response(200, json={"id": "would-be-duplicate"}),
        ]
    )

    with pytest.raises(NetworkError):
        client.create_page("TEST", "Unique run title", "<p>body</p>")

    assert route.call_count == 1


# ---------------------------------------------------------------------------
# update_page
# ---------------------------------------------------------------------------


@respx.mock
def test_update_page(client: ConfluenceClient) -> None:
    expected = _load("update-page-expected.json")
    route = respx.put(f"{BASE_URL}/rest/api/content/12345678").mock(return_value=httpx.Response(200, json=expected))

    result = client.update_page(
        "12345678",
        "Updated Title",
        "<p>Updated content</p>",
        version_number=3,
        reason="Explain the edit",
        minor_edit=True,
    )

    assert result["id"] == "12345678"
    assert result["version"]["number"] == 3
    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body["version"]["number"] == 3
    assert sent_body["version"]["message"] == "Explain the edit"
    assert sent_body["version"]["minorEdit"] is True
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


@respx.mock
def test_upload_attachment_raw_uses_explicit_remote_filename(client: ConfluenceClient, tmp_path: Path) -> None:
    local_file = tmp_path / "a_b.png"
    local_file.write_bytes(b"\x89PNG\r\n")
    route = respx.post(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "att401", "title": "a*b.png"}]})
    )

    client._upload_attachment_raw("100", local_file, filename="a*b.png")

    content = route.calls[0].request.content
    assert b'filename="a*b.png"' in content
    assert b'filename="a_b.png"' not in content


@respx.mock
def test_upload_attachment_raw_versions_existing_attachment_by_id(client: ConfluenceClient, tmp_path: Path) -> None:
    local_file = tmp_path / "local.png"
    local_file.write_bytes(b"new")
    route = respx.post(f"{BASE_URL}/rest/api/content/100/child/attachment/att-1/data").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "att-1", "version": {"number": 5}}]})
    )

    client._upload_attachment_raw("100", local_file, filename="remote.png", attachment_id="att-1")

    assert route.called
    assert b'filename="remote.png"' in route.calls[0].request.content


@respx.mock
def test_upload_attachment_raw_does_not_retry_ambiguous_multipart_post(
    client: ConfluenceClient, tmp_path: Path
) -> None:
    local_file = tmp_path / "local.png"
    local_file.write_bytes(b"new")
    route = respx.post(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(
        side_effect=[
            httpx.Response(500, text="unknown upload outcome"),
            httpx.Response(200, json={"results": [{"id": "att-2", "version": {"number": 1}}]}),
        ]
    )

    with pytest.raises(NetworkError) as exc_info:
        client._upload_attachment_raw("100", local_file, filename="remote.png")

    assert exc_info.value.http_status == 500
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# upload_attachments_batch — replace mode skips existing check
# ---------------------------------------------------------------------------


@respx.mock
def test_upload_attachments_batch_replace_versions_the_stored_file(client: ConfluenceClient, tmp_path: Path) -> None:
    """`replace` posts a new version of the stored attachment.

    This test used to assert the opposite -- that `replace` makes no GET and goes
    straight to the create endpoint -- and its comment called that the intent. It was
    the reason the defect survived: on a page that already held the filename, the create
    endpoint answers 400 and the flag the caller passed is never mentioned. The fixture
    had no existing attachment, so the case it was named for was never exercised.

    `replace` and `version` are one operation. Confluence Server/DC cannot overwrite
    attachment bytes without adding a version, so both spellings do the same thing
    rather than `replace` deleting history to look distinct.
    """

    file_a = tmp_path / "fileA.txt"
    file_a.write_text("content A")

    respx.get(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "att500", "title": "fileA.txt"}], "size": 1})
    )
    version = respx.post(f"{BASE_URL}/rest/api/content/100/child/attachment/att500/data").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "att500", "title": "fileA.txt"}]})
    )
    create = respx.post(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "att999", "title": "fileA.txt"}]})
    )

    results = client.upload_attachments_batch("100", [file_a], if_exists="replace")

    assert version.call_count == 1
    assert not create.called, "a stored filename must never be re-created"
    assert results[0]["results"][0]["id"] == "att500"


@respx.mock
def test_upload_attachment_with_a_stored_id_posts_a_version(client: ConfluenceClient, tmp_path) -> None:
    """The single-file path needed this too, and had no way to ask for it.

    `upload_attachments_batch` learned to post to the version endpoint for a name the
    page already holds. `upload_assets` -- the path behind every body publish -- called
    `upload_attachment`, which could only create, so the second publish of a document
    carrying a picture was answered `400 Cannot add a new attachment with same file
    name as an existing attachment` and the body update went down with it.
    """

    source = tmp_path / "diagram.png"
    source.write_bytes(b"png bytes")

    version = respx.post(f"{BASE_URL}/rest/api/content/100/child/attachment/att-7/data").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "att-7", "title": "diagram.png"}]})
    )
    create = respx.post(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "att-9", "title": "diagram.png"}]})
    )

    client.upload_attachment("100", source, attachment_id="att-7")

    assert version.call_count == 1
    assert not create.called, "a stored filename must never be re-created"


@respx.mock
def test_upload_attachment_without_a_stored_id_still_creates(client: ConfluenceClient, tmp_path) -> None:
    """A name the page does not hold has no version to add to."""

    source = tmp_path / "fresh.png"
    source.write_bytes(b"png bytes")

    create = respx.post(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "att-9", "title": "fresh.png"}]})
    )

    client.upload_attachment("100", source)

    assert create.call_count == 1


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


@respx.mock
def test_upload_attachments_batch_refuses_an_unknown_if_exists(client: ConfluenceClient, tmp_path: Path) -> None:
    """A misspelled mode must be refused, not treated as "version".

    The batch uploader branched on `if_exists == "skip"` and sent everything else to the
    version endpoint. That reads as a two-way choice, but the CLI takes a free string, so
    `--if-exists=typo` became a new version of a file the caller never meant to touch --
    an unexpected remote write produced by a typo. Reproduced before the fix: with one
    stored attachment `att-1`, `if_exists="typo"` posted to `att-1`'s data endpoint.

    Refused before any request, so a wrong mode cannot cost a version even on a page the
    caller is allowed to write.
    """

    file = tmp_path / "diagram.png"
    file.write_bytes(b"bytes")
    listing = respx.get(f"{BASE_URL}/rest/api/content/100/child/attachment")
    create = respx.post(f"{BASE_URL}/rest/api/content/100/child/attachment")

    with pytest.raises(ValidationError) as refused:
        client.upload_attachments_batch("100", [file], if_exists="typo")

    assert refused.value.context["reason"] == "unknown_if_exists"
    assert not listing.called and not create.called, "a rejected mode must not reach the network"


@respx.mock
def test_upload_attachments_batch_accepts_every_documented_mode(client: ConfluenceClient, tmp_path: Path) -> None:
    """And the three the CLI advertises all still work, so the guard is not a blanket no."""

    file = tmp_path / "diagram.png"
    file.write_bytes(b"bytes")
    respx.get(f"{BASE_URL}/rest/api/content/100/child/attachment").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "att-1", "title": "diagram.png"}], "size": 1})
    )
    respx.post(f"{BASE_URL}/rest/api/content/100/child/attachment/att-1/data").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "att-1"}]})
    )

    for mode in ("skip", "version", "replace"):
        client.upload_attachments_batch("100", [file], if_exists=mode)
