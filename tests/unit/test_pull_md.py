from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from atlassian_skills.confluence.models import Attachment, Page, PageVersion
from atlassian_skills.confluence.pull_md import PullResult, pull_md


def _make_page(body_storage: str, title: str = "Test Page", version: int = 1) -> Page:
    return Page(
        id="12345",
        title=title,
        body_storage=body_storage,
        version=PageVersion(number=version),
    )


def _make_client(page: Page) -> MagicMock:
    client = MagicMock()
    client.get_page.return_value = page
    return client


class TestPullMdReturnsContent:
    def test_returns_pull_result(self) -> None:
        """pull_md returns PullResult with markdown, version, title."""
        page = _make_page(body_storage="<h1>Hello</h1><p>World</p>", version=5, title="My Page")
        client = _make_client(page)

        result = pull_md(client, "12345")

        assert isinstance(result, PullResult)
        assert result.version == 5
        assert result.title == "My Page"
        assert "Hello" in result.markdown
        assert "World" in result.markdown


class TestPullMdWritesFile:
    def test_writes_to_output_path(self, tmp_path: Path) -> None:
        """When output_path is given, file is created with md content."""
        page = _make_page(body_storage="<h1>Title</h1><p>Body text</p>", version=3)
        client = _make_client(page)
        out = tmp_path / "subdir" / "output.md"

        result = pull_md(client, "12345", output_path=out)

        assert out.exists()
        file_content = out.read_text(encoding="utf-8")
        assert file_content == result.markdown
        assert "Title" in file_content
        assert result.version == 3


class TestPullMdPassthrough:
    def test_passthrough_prefix_accepted(self) -> None:
        """passthrough_prefixes parameter is accepted without error."""
        page = _make_page(body_storage="<p>Content</p>")
        client = _make_client(page)

        result = pull_md(client, "12345", passthrough_prefixes=["ac:"])

        assert isinstance(result, PullResult)


class TestPullMdJsonVersion:
    def test_version_in_result(self) -> None:
        """PullResult includes version from page metadata."""
        page = _make_page(body_storage="<p>Hello</p>", version=42, title="Versioned")
        client = _make_client(page)

        result = pull_md(client, "12345")

        assert result.version == 42
        assert result.title == "Versioned"


class TestPullMdResolveAssetsSidecar:
    def test_sidecar_downloads_and_rewrites(self, tmp_path: Path) -> None:
        """Sidecar mode downloads attachments and rewrites image links."""
        md_with_marker = (
            '# Page\n\n'
            '![diagram](diagram.png)<!-- cfxmark:asset src="diagram.png" -->\n\n'
            'Some text\n'
        )
        # We need to provide storage that converts to md_with_marker.
        # Since we can't easily reverse cfxmark, we mock the conversion by
        # providing the md directly via passthrough-free path.
        # Instead, we test the _resolve_assets_sidecar function directly.
        from atlassian_skills.confluence.pull_md import _resolve_assets_sidecar

        client = MagicMock()
        client.list_attachments.return_value = [
            Attachment(id="att-001", title="diagram.png"),
        ]
        client.download_attachment.return_value = tmp_path / "assets" / "diagram.png"

        asset_dir = tmp_path / "assets"
        md_path = tmp_path / "page.md"

        result = _resolve_assets_sidecar(client, "12345", md_with_marker, asset_dir, md_path)

        client.download_attachment.assert_called_once_with("att-001", asset_dir / "diagram.png")
        assert "assets/diagram.png" in result
        # Marker is preserved
        assert '<!-- cfxmark:asset src="diagram.png" -->' in result

    def test_sidecar_preserves_non_asset_content(self, tmp_path: Path) -> None:
        """Non-asset content is unchanged by sidecar resolution."""
        md_no_markers = "# Hello\n\nJust text, no images.\n"
        from atlassian_skills.confluence.pull_md import _resolve_assets_sidecar

        client = MagicMock()
        asset_dir = tmp_path / "assets"

        result = _resolve_assets_sidecar(client, "12345", md_no_markers, asset_dir)

        assert result == md_no_markers
        client.list_attachments.assert_not_called()

    def test_sidecar_skips_unknown_attachments(self, tmp_path: Path) -> None:
        """Attachments not found on server are skipped without error."""
        md_with_marker = '![img](missing.png)<!-- cfxmark:asset src="missing.png" -->\n'
        from atlassian_skills.confluence.pull_md import _resolve_assets_sidecar

        client = MagicMock()
        client.list_attachments.return_value = []  # no attachments on server

        asset_dir = tmp_path / "assets"
        result = _resolve_assets_sidecar(client, "12345", md_with_marker, asset_dir)

        client.download_attachment.assert_not_called()
        # Original link preserved (not rewritten since attachment not found)
        assert "missing.png" in result
