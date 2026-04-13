from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from atlassian_skills.confluence.models import Page, PageVersion
from atlassian_skills.confluence.push_md import push_md
from atlassian_skills.core.errors import StaleError


def _make_page(body_storage: str, version: int = 1, title: str = "Test Page") -> Page:
    return Page(
        id="12345",
        title=title,
        body_storage=body_storage,
        version=PageVersion(number=version),
    )


def _make_client(page: Page, update_result: dict | None = None) -> MagicMock:
    client = MagicMock()
    client.get_page.return_value = page
    client.update_page.return_value = update_result or {"id": "12345", "version": {"number": 2}}
    client.upload_attachment.return_value = {"id": "att1"}
    client.upload_attachments_batch.return_value = [{"id": "att1"}]
    return client


class TestPushMdNoChange:
    def test_returns_no_change_dict_when_content_identical(self) -> None:
        """When converted md matches server body, return no_change dict with version."""
        from atlassian_skills.core.format.markdown import md_to_confluence_storage

        md_content = "# Hello\n\nWorld"
        storage = md_to_confluence_storage(md_content)
        page = _make_page(body_storage=storage, version=5)
        client = _make_client(page)

        result = push_md(client, "12345", md_content)

        assert result["status"] == "no_change"
        assert result["page_id"] == "12345"
        assert result["version"] == 5
        client.update_page.assert_not_called()


class TestPushMdUpdates:
    def test_calls_update_when_content_different(self) -> None:
        """When converted md differs from server, update_page is called."""
        page = _make_page(body_storage="<p>Old content</p>", version=2)
        client = _make_client(page)

        result = push_md(client, "12345", "# New content\n\nDifferent text")

        assert result["status"] == "updated"
        assert result["version"] == 3
        assert result["page_id"] == "12345"
        client.update_page.assert_called_once()
        call_kwargs = client.update_page.call_args
        assert call_kwargs.kwargs["page_id"] == "12345"
        assert call_kwargs.kwargs["version_number"] == 3


class TestPushMdDryRun:
    def test_dry_run_does_not_call_update(self) -> None:
        """dry_run=True returns preview dict without calling update_page."""
        page = _make_page(body_storage="<p>Old content</p>", version=5)
        client = _make_client(page)

        result = push_md(client, "12345", "# Different content", dry_run=True)

        assert result is not None
        assert result["status"] == "dry_run"
        assert result["page_id"] == "12345"
        assert result["dry_run"] is True
        assert result["would_update"] is True
        assert result["version"] == 6
        client.update_page.assert_not_called()


class TestPushMdWithAttachments:
    def test_uploads_attachments_via_batch(self, tmp_path: Path) -> None:
        """Attachments are uploaded via batch upload after page update."""
        att1 = tmp_path / "image.png"
        att1.write_bytes(b"fake png")
        att2 = tmp_path / "doc.pdf"
        att2.write_bytes(b"fake pdf")

        page = _make_page(body_storage="<p>Old</p>", version=1)
        client = _make_client(page)

        result = push_md(
            client,
            "12345",
            "# Updated",
            attachments=[att1, att2],
        )

        assert result["status"] == "updated"
        client.upload_attachments_batch.assert_called_once()
        call_args = client.upload_attachments_batch.call_args
        assert call_args.args[0] == "12345"
        assert len(call_args.args[1]) == 2
        assert call_args.kwargs["if_exists"] == "replace"


class TestPushMdAttachmentIfExists:
    def test_skip_mode_passes_to_batch(self, tmp_path: Path) -> None:
        """attachment_if_exists='skip' is forwarded to upload_attachments_batch."""
        att = tmp_path / "img.png"
        att.write_bytes(b"fake")

        page = _make_page(body_storage="<p>Old</p>", version=1)
        client = _make_client(page)

        push_md(client, "12345", "# New", attachments=[att], attachment_if_exists="skip")

        call_args = client.upload_attachments_batch.call_args
        assert call_args.kwargs["if_exists"] == "skip"


class TestPushMdIfVersion:
    def test_stale_version_raises(self) -> None:
        """if_version mismatch raises StaleError."""
        page = _make_page(body_storage="<p>Content</p>", version=16)
        client = _make_client(page)

        import pytest

        with pytest.raises(StaleError) as exc_info:
            push_md(client, "12345", "# Content", if_version=15)

        assert exc_info.value.context is not None
        assert exc_info.value.context["server_version"] == 16
        assert exc_info.value.context["expected_version"] == 15

    def test_matching_version_proceeds(self) -> None:
        """if_version matching server version proceeds normally."""
        page = _make_page(body_storage="<p>Old</p>", version=15)
        client = _make_client(page)

        result = push_md(client, "12345", "# New content", if_version=15)

        assert result["status"] == "updated"
        assert result["version"] == 16

    def test_none_if_version_skips_check(self) -> None:
        """if_version=None (default) skips version check."""
        page = _make_page(body_storage="<p>Old</p>", version=99)
        client = _make_client(page)

        result = push_md(client, "12345", "# New", if_version=None)

        assert result["status"] == "updated"
