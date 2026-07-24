from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from atlassian_skills.confluence.models import Page, PageVersion
from atlassian_skills.confluence.push_md import push_md
from atlassian_skills.core.errors import StaleError, ValidationError


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
        assert result["put_count"] == 0  # uniform with the stateless/managed no_change receipt
        client.update_page.assert_not_called()

    def test_ignores_server_only_storage_normalization(self) -> None:
        from atlassian_skills.core.format.markdown import md_to_confluence_storage

        md_content = "# Hello\n\nWorld"
        storage = md_to_confluence_storage(md_content)
        server_storage = storage.replace("<p>", '<p class="server-normalized">', 1)
        assert storage != server_storage
        client = _make_client(_make_page(body_storage=server_storage, version=5))

        result = push_md(client, "12345", md_content)

        assert result["status"] == "no_change"
        client.update_page.assert_not_called()

    def test_code_block_whitespace_difference_is_not_normalized_away(self) -> None:
        from atlassian_skills.core.format.markdown import md_to_confluence_storage

        server_storage = md_to_confluence_storage("```python\nx = 1\n```\n")
        client = _make_client(_make_page(body_storage=server_storage, version=5))

        result = push_md(client, "12345", "```python\nx  = 1\n```\n", dry_run=True)

        assert result["status"] == "dry_run"
        assert result["would_update"] is True

    def test_code_block_trailing_newline_difference_is_not_normalized_away(self) -> None:
        from atlassian_skills.core.format.markdown import md_to_confluence_storage

        server_storage = md_to_confluence_storage("```text\nvalue\n```\n")
        client = _make_client(_make_page(body_storage=server_storage, version=5))

        result = push_md(client, "12345", "```text\nvalue\n\n```\n", dry_run=True)

        assert result["status"] == "dry_run"
        assert result["would_update"] is True


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

    @pytest.mark.parametrize(
        "marker",
        [
            "<!-- atls:mode=readable push=blocked -->",
            "<!-- atls:profile=readable push-safe=false -->",
            "<!-- cfxmark:unsupported kind=block push-safe=false -->",
        ],
    )
    def test_rejects_explicitly_unsafe_markdown(self, marker: str) -> None:
        client = _make_client(_make_page(body_storage="<p>Old</p>"))

        with pytest.raises(ValidationError):
            push_md(client, "12345", f"{marker}\n\nContent")

        client.get_page.assert_not_called()
        client.update_page.assert_not_called()

    def test_rejects_readable_conversion_output(self) -> None:
        from atlassian_skills.core.format.markdown import confluence_storage_to_md

        md_content = confluence_storage_to_md("<p>Read only</p>", profile="readable")
        client = _make_client(_make_page(body_storage="<p>Old</p>"))

        with pytest.raises(ValidationError):
            push_md(client, "12345", md_content)

        client.get_page.assert_not_called()
        client.update_page.assert_not_called()

    def test_rejects_update_when_current_server_content_is_unsupported(self) -> None:
        server_body = '<ac:structured-macro xmlns:ac="http://atlassian.com/content" ac:name="sample-unknown"/>'
        client = _make_client(_make_page(body_storage=server_body))

        with pytest.raises(ValidationError, match="current Confluence page"):
            push_md(client, "12345", "# Replacement")

        client.get_page.assert_called_once_with("12345")
        client.update_page.assert_not_called()

    def test_rejects_update_when_current_page_contains_layout(self) -> None:
        storage = (
            '<ac:layout xmlns:ac="http://atlassian.com/content">'
            '<ac:layout-section ac:type="two_equal">'
            "<ac:layout-cell><p>left</p></ac:layout-cell>"
            "<ac:layout-cell><p>right</p></ac:layout-cell>"
            "</ac:layout-section></ac:layout>"
        )
        client = _make_client(_make_page(body_storage=storage, version=5))

        with pytest.raises(ValidationError, match="unsupported content"):
            push_md(client, "12345", "changed\n")

        client.update_page.assert_not_called()

    def test_unmanaged_push_rejects_html_comment_as_content_loss(self) -> None:
        """An unmanaged push-md has no consent mechanism, so a user HTML comment --
        now a classified content loss (``html-comment-dropped``), consistent with any
        other stripped construct (e.g. ``<span>``) -- fails closed rather than silently
        dropping it. The managed path is where such a loss is resolved through consent."""
        client = _make_client(_make_page(body_storage="<p>old</p>", version=5))

        with pytest.raises(ValidationError) as exc_info:
            push_md(client, "12345", "<!-- ordinary comment -->\n\nnew\n", dry_run=True)

        assert exc_info.value.context["push_safe"] is False
        assert any("HTML comment" in loss for loss in exc_info.value.context["losses"])

    @pytest.mark.parametrize(
        "documented_marker",
        [
            "<!-- atls:mode=readable push=blocked -->",
            "<!-- cfxmark:unsupported kind=block push-safe=false -->",
        ],
    )
    def test_marker_documented_in_code_fence_does_not_block_push(self, documented_marker: str) -> None:
        client = _make_client(_make_page(body_storage="<p>Old</p>"))
        markdown = f"# Marker documentation\n\n```markdown\n{documented_marker}\n```\n"

        result = push_md(client, "12345", markdown, dry_run=True)

        assert result["status"] == "dry_run"

    @pytest.mark.parametrize(
        "markdown",
        [
            "Use `<!-- atls:mode=readable push=blocked -->` as an example.\n",
            "    <!-- cfxmark:unsupported kind=block push-safe=false -->\n",
        ],
    )
    def test_marker_documented_in_other_code_forms_does_not_block_push(self, markdown: str) -> None:
        client = _make_client(_make_page(body_storage="<p>Old</p>"))

        result = push_md(client, "12345", markdown, dry_run=True)

        assert result["status"] == "dry_run"

    def test_real_marker_after_fence_with_trailing_spaces_still_blocks_push(self) -> None:
        client = _make_client(_make_page(body_storage="<p>Old</p>"))
        markdown = (
            "```markdown\n"
            "<!-- atls:mode=readable push=blocked -->\n"
            "```   \n"
            "<!-- cfxmark:unsupported kind=block push-safe=false -->\n"
        )

        with pytest.raises(ValidationError, match="unsupported Confluence content"):
            push_md(client, "12345", markdown, dry_run=True)


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
