from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import cfxmark
import pytest

from atlassian_skills.confluence.models import Attachment, Page, PageVersion
from atlassian_skills.confluence.pull_md import PullResult, pull_md, pull_pages_batch
from atlassian_skills.core.errors import AtlasError, ValidationError


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
        assert result.push_safe is True
        assert result.losses == ()

    def test_unsupported_content_is_explicitly_not_push_safe(self) -> None:
        # A body-carrying macro: body-less macros now round-trip byte-exactly
        # through cfxmark's managed opaque payload transport and are push-safe.
        page = _make_page(
            body_storage=(
                '<p xmlns:ac="http://atlassian.com/content">Text<ac:structured-macro ac:name="sample-unknown">'
                "<ac:rich-text-body><p>inner</p></ac:rich-text-body></ac:structured-macro></p>"
            )
        )
        client = _make_client(page)

        result = pull_md(client, "12345")

        assert result.push_safe is False
        assert "cfxmark:unsupported" in result.markdown
        assert 'macro="sample-unknown"' in result.markdown
        assert "<ac:structured-macro" not in result.markdown

    def test_inline_jira_and_paragraph_toc_are_editable(self) -> None:
        page = _make_page(
            body_storage=(
                '<p><ac:structured-macro ac:name="toc"/></p>'
                '<p>Related: <ac:structured-macro ac:name="jira">'
                '<ac:parameter ac:name="key">DOC-123</ac:parameter>'
                "</ac:structured-macro></p>"
            )
        )
        client = _make_client(page)

        result = pull_md(client, "12345")

        assert result.push_safe is True
        assert "::: toc" in result.markdown
        assert '{{jira key="DOC-123"}}' in result.markdown
        assert "cfxmark:unsupported" not in result.markdown

    def test_nested_table_remains_not_push_safe(self) -> None:
        page = _make_page(
            body_storage=(
                "<table><tbody><tr><td>Outer</td><td>"
                "<table><tbody><tr><td>Inner</td></tr></tbody></table>"
                "</td></tr></tbody></table>"
            )
        )
        client = _make_client(page)

        result = pull_md(client, "12345")

        assert result.push_safe is False
        assert "cfxmark:unsupported" in result.markdown


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

    def test_reserved_passthrough_prefix_raises_atlas_error(self, tmp_path: Path) -> None:
        """An invalid passthrough prefix must surface as an AtlasError so the CLI
        renders a JSON envelope (exit 7) instead of letting a bare
        ManagedManifestError escape as a Rich traceback."""
        page = _make_page(body_storage="<p>Content</p>")
        client = _make_client(page)

        with pytest.raises(ValidationError) as excinfo:
            pull_md(
                client,
                "12345",
                output_path=tmp_path / "page.md",
                passthrough_prefixes=["atls:owned"],
                portable=True,
            )
        assert isinstance(excinfo.value, AtlasError)
        assert (excinfo.value.context or {}).get("reason") == "reserved_passthrough_prefix"

    def test_batch_reserved_passthrough_prefix_raises_atlas_error(self, tmp_path: Path) -> None:
        """pull_pages_batch shares the same passthrough guard as pull_md."""
        page = _make_page(body_storage="<p>Content</p>")
        client = _make_client(page)

        with pytest.raises(ValidationError) as excinfo:
            pull_pages_batch(
                client,
                ["12345"],
                tmp_path,
                passthrough_prefixes=["cfxmark:x"],
                portable=True,
            )
        assert isinstance(excinfo.value, AtlasError)
        assert (excinfo.value.context or {}).get("reason") == "reserved_passthrough_prefix"


class TestPullMdJsonVersion:
    def test_version_in_result(self) -> None:
        """PullResult includes version from page metadata."""
        page = _make_page(body_storage="<p>Hello</p>", version=42, title="Versioned")
        client = _make_client(page)

        result = pull_md(client, "12345")

        assert result.version == 42
        assert result.title == "Versioned"


class TestPullMdResolveAssetsSidecar:
    def test_sidecar_downloads_and_rewrites(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sidecar mode downloads attachments and rewrites image links."""
        md_with_marker = '# Page\n\n![diagram](diagram.png)<!-- cfxmark:asset src="diagram.png" -->\n\nSome text\n'
        # We need to provide storage that converts to md_with_marker.
        # Since we can't easily reverse cfxmark, we mock the conversion by
        # providing the md directly via passthrough-free path.
        # Instead, we test the _resolve_assets_sidecar function directly.
        from atlassian_skills.confluence.pull_md import _resolve_assets_sidecar

        client = MagicMock()
        client.list_attachments.return_value = [
            Attachment(id="att-001", title="diagram.png"),
        ]
        client.fetch_attachment_bytes.return_value = b"image-bytes"

        asset_dir = tmp_path / "assets"
        md_path = tmp_path / "page.md"

        result = _resolve_assets_sidecar(client, "12345", md_with_marker, asset_dir, md_path)

        client.fetch_attachment_bytes.assert_called_once_with("att-001", None)
        assert (asset_dir / "diagram.png").read_bytes() == b"image-bytes"
        assert "assets/diagram.png" in result
        # Managed Markdown contains only the final binding marker; asset
        # identity lives in the global state database.
        assert '<!-- cfxmark:asset src="diagram.png" -->' not in result

    def test_sidecar_preserves_image_metadata(self, tmp_path: Path) -> None:
        md_with_marker = (
            "![diagram](diagram.png#cfxmark:w=320,h=180,thumbnail=1,align=center)"
            '<!-- cfxmark:asset src="diagram.png" -->\n'
        )
        from atlassian_skills.confluence.pull_md import _resolve_assets_sidecar

        client = MagicMock()
        client.list_attachments.return_value = [Attachment(id="att-001", title="diagram.png")]
        client.fetch_attachment_bytes.return_value = b"image-bytes"

        result = _resolve_assets_sidecar(
            client,
            "12345",
            md_with_marker,
            tmp_path / "assets",
            tmp_path / "page.md",
        )

        assert "assets/diagram.png#cfxmark:w=320,h=180,thumbnail=1,align=center" in result

    def test_sidecar_preserves_canonical_image_comment_metadata(self, tmp_path: Path) -> None:
        md_with_marker = (
            "![diagram](diagram.png)"
            "<!-- cfxmark:img w=320 h=180 thumbnail=1 align=center -->"
            '<!-- cfxmark:asset src="diagram.png" -->\n'
        )
        from atlassian_skills.confluence.pull_md import _resolve_assets_sidecar

        client = MagicMock()
        client.list_attachments.return_value = [Attachment(id="att-001", title="diagram.png")]
        client.fetch_attachment_bytes.return_value = b"image-bytes"

        result = _resolve_assets_sidecar(
            client,
            "12345",
            md_with_marker,
            tmp_path / "assets",
            tmp_path / "page.md",
        )

        assert "![diagram](assets/diagram.png)<!-- cfxmark:img w=320 h=180 thumbnail=1 align=center -->" in result
        assert "cfxmark:asset" not in result

    def test_sidecar_percent_encodes_unsafe_path_characters(self, tmp_path: Path) -> None:
        original_name = "Screen Shot #1%.png"
        md_with_marker = f'![diagram](remote)<!-- cfxmark:asset src="{original_name}" -->\n'
        from atlassian_skills.confluence.pull_md import _resolve_assets_sidecar

        client = MagicMock()
        client.list_attachments.return_value = [Attachment(id="att-001", title=original_name)]
        client.fetch_attachment_bytes.return_value = b"image-bytes"

        result = _resolve_assets_sidecar(
            client,
            "12345",
            md_with_marker,
            tmp_path / "asset folder",
            tmp_path / "page.md",
        )

        assert "asset%20folder/Screen%20Shot%20%231%25.png" in result

    def test_sidecar_rewrites_angle_destination_with_parenthesis(self, tmp_path: Path) -> None:
        original_name = "diagram (final).png"
        md_with_marker = f'![diagram](<remote)>)<!-- cfxmark:asset src="{original_name}" -->\n'
        from atlassian_skills.confluence.pull_md import _resolve_assets_sidecar

        client = MagicMock()
        client.list_attachments.return_value = [Attachment(id="att-001", title=original_name)]
        client.fetch_attachment_bytes.return_value = b"image-bytes"

        result = _resolve_assets_sidecar(
            client,
            "12345",
            md_with_marker,
            tmp_path / "assets",
            tmp_path / "page.md",
        )

        assert "assets/diagram%20%28final%29.png" in result

    def test_sidecar_preserves_metadata_from_angle_destination(self, tmp_path: Path) -> None:
        original_name = "wide diagram.png"
        md_with_marker = (
            f'![diagram](<remote file#cfxmark:w=320,h=180,align=center>)<!-- cfxmark:asset src="{original_name}" -->\n'
        )
        from atlassian_skills.confluence.pull_md import _resolve_assets_sidecar

        client = MagicMock()
        client.list_attachments.return_value = [Attachment(id="att-001", title=original_name)]
        client.fetch_attachment_bytes.return_value = b"image-bytes"

        result = _resolve_assets_sidecar(
            client,
            "12345",
            md_with_marker,
            tmp_path / "assets",
            tmp_path / "page.md",
        )

        assert "assets/wide%20diagram.png#cfxmark:w=320,h=180,align=center" in result
        assert "asset folder/Screen Shot #1%.png" not in result

    def test_sidecar_preserves_non_asset_content(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

        client.fetch_attachment_bytes.assert_not_called()
        # Original link preserved (not rewritten since attachment not found)
        assert "missing.png" in result

    def test_repeated_marker_fetches_and_stages_one_asset(self, tmp_path: Path) -> None:
        from atlassian_skills.confluence.pull_md import _resolve_assets_sidecar

        marker = '![img](old.png)<!-- cfxmark:asset src="diagram.png" -->'
        client = MagicMock()
        client.list_attachments.return_value = [Attachment(id="att-001", title="diagram.png")]
        client.fetch_attachment_bytes.return_value = b"image"

        result = _resolve_assets_sidecar(client, "12345", f"{marker}\n{marker}\n", tmp_path / "assets")

        client.fetch_attachment_bytes.assert_called_once_with("att-001", None)
        assert result.count("assets/diagram.png") == 2


class TestPullPagesBatch:
    def test_two_pages_share_one_asset_commit_and_use_page_id_directories(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import atlassian_skills.confluence.pull_md as pull_module

        page_one = _make_page("unused", title="같은 제목 — (test)", version=3)
        page_two = _make_page("unused", title="같은 제목 — (test)", version=4)
        client = MagicMock()
        client.get_page.side_effect = [page_one, page_two]
        client.list_attachments.side_effect = [
            [Attachment(id="att-1", title="image.png")],
            [Attachment(id="att-2", title="image.png")],
        ]
        client.fetch_attachment_bytes.side_effect = [b"first", b"second"]
        marker = '![img](remote#cfxmark:w=320,h=180,thumbnail=1,align=center)<!-- cfxmark:asset src="image.png" -->'
        monkeypatch.setattr(
            pull_module,
            "_convert_storage",
            lambda *_args: cfxmark.ConversionResult(markdown=marker),
        )
        real_commit = pull_module.AttachmentWriteBatch.commit
        commits: list[int] = []

        def capture_commit(batch: object) -> list[Path]:
            commits.append(1)
            return real_commit(batch)  # type: ignore[arg-type]

        monkeypatch.setattr(pull_module.AttachmentWriteBatch, "commit", capture_commit)

        results = pull_pages_batch(client, ["100", "200"], tmp_path)

        assert commits == [1]
        assert [result.assets for result in results] == [1, 1]
        assert results[0].path.parent != results[1].path.parent
        assert results[0].path.parent.name.endswith("--100")
        assert results[1].path.parent.name.endswith("--200")
        expected_link = "assets/image.png#cfxmark:w=320,h=180,thumbnail=1,align=center"
        assert expected_link in results[0].path.read_text(encoding="utf-8")
        assert expected_link in results[1].path.read_text(encoding="utf-8")
        assert (results[0].path.parent / "assets" / "image.png").read_bytes() == b"first"
        assert (results[1].path.parent / "assets" / "image.png").read_bytes() == b"second"

    def test_asset_failure_publishes_no_markdown(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.confluence.pull_md as pull_module

        client = MagicMock()
        client.get_page.side_effect = [_make_page("unused", title="first"), _make_page("unused", title="second")]
        client.list_attachments.side_effect = [
            [Attachment(id="att-1", title="image.png")],
            [Attachment(id="att-2", title="image.png")],
        ]
        client.fetch_attachment_bytes.side_effect = [b"first", AtlasError("download failed")]
        marker = '![img](remote)<!-- cfxmark:asset src="image.png" -->'
        monkeypatch.setattr(
            pull_module,
            "_convert_storage",
            lambda *_args: cfxmark.ConversionResult(markdown=marker),
        )

        with pytest.raises(AtlasError, match="download failed"):
            pull_pages_batch(client, ["100", "200"], tmp_path)

        assert list(tmp_path.rglob("*.md")) == []
        assert list(tmp_path.rglob(".atls-*.part")) == []
