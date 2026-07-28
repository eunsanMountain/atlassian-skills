from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import cfxmark
import pytest

from atlassian_skills.confluence.pull_md import pull_md
from atlassian_skills.core.errors import ConflictError
from atlassian_skills.core.format.markdown import confluence_storage_to_md_result
from atlassian_skills.core.managed_manifest import extract_asset_records, parse_managed_document


class FakeClient:
    base_url = "HTTPS://EXAMPLE.COM:443/confluence/"

    def __init__(self, storage: str = "<p>Synthetic</p>", *, version: int = 7) -> None:
        self.storage = storage
        self.version = version
        self.calls = 0

    def get_page(self, page_id: str) -> SimpleNamespace:
        self.calls += 1
        return SimpleNamespace(
            id=page_id,
            title="Synthetic Page",
            body_storage=self.storage,
            version=SimpleNamespace(number=self.version),
        )


class FakeAssetClient(FakeClient):
    def __init__(self) -> None:
        super().__init__(
            '<p><ac:image xmlns:ac="http://atlassian.com/content" '
            'xmlns:ri="http://atlassian.com/resource/identifier"><ri:attachment '
            'ri:filename="road map#v1?100%-表.png"/></ac:image></p>'
        )
        self.attachment = SimpleNamespace(
            id="att-1",
            title="road map#v1?100%-表.png",
            version=SimpleNamespace(number=4),
            media_type="image/png",
            links=SimpleNamespace(download="/download/att-1"),
        )

    def list_attachments(self, page_id: str) -> list[SimpleNamespace]:
        assert page_id == "123456"
        return [self.attachment]

    def fetch_attachment_bytes(self, attachment_id: str, download_link: str | None) -> bytes:
        assert (attachment_id, download_link) == ("att-1", "/download/att-1")
        return b"synthetic-image"


class FakeOpaqueAssetClient(FakeAssetClient):
    def __init__(self) -> None:
        FakeClient.__init__(
            self,
            '<pre><code><ac:image xmlns:ac="http://atlassian.com/content" '
            'xmlns:ri="http://atlassian.com/resource/identifier"><ri:attachment '
            'ri:filename="road map#v1?100%-表.png"/></ac:image></code></pre>',
        )
        self.attachment = SimpleNamespace(
            id="att-1",
            title="road map#v1?100%-表.png",
            version=SimpleNamespace(number=4),
            media_type="image/png",
            links=SimpleNamespace(download="/download/att-1"),
        )


class FakeDetachedAssetClient(FakeAssetClient):
    def __init__(self) -> None:
        FakeClient.__init__(
            self,
            "<table><tbody><tr><th><p>alpha beta gamma delta heading words</p></th></tr>"
            '<tr><td><p><ac:link xmlns:ac="http://atlassian.com/content" '
            'xmlns:ri="http://atlassian.com/resource/identifier"><ri:attachment '
            'ri:filename="road map#v1?100%-表.png"/></ac:link></p></td></tr></tbody></table>'
            "<p>closing paragraph of the page body text here</p>",
        )
        self.attachment = SimpleNamespace(
            id="att-1",
            title="road map#v1?100%-表.png",
            version=SimpleNamespace(number=4),
            media_type="image/png",
            links=SimpleNamespace(download="/download/att-1"),
        )


def test_portable_pull_uses_cfxmark_managed_projection_without_sqlite(tmp_path: Path) -> None:
    output = tmp_path / "portable.md"

    result = pull_md(
        FakeClient("<p>before<!-- synthetic comment -->after</p>"),
        "123456",
        output_path=output,
        portable=True,
    )

    managed = output.read_text(encoding="utf-8")
    parsed = parse_managed_document(managed)
    assert parsed.manifest.page == "123456"
    # The pull stamps the installed converter; pinning the literal turns every
    # cfxmark release into a test edit and hides real drift behind a known failure.
    assert parsed.manifest.converter == f"cfxmark/{cfxmark.__version__}"
    assert parsed.manifest.profile == "markdown-first"
    assert "<!-- cfxmark:notice " in parsed.content
    assert result.status == "pulled_with_migrations"
    assert result.migration_report is not None
    [occurrence] = result.migration_report["occurrences"]
    assert occurrence["occurrence_id"].startswith("mig_occ_sha256:")
    assert occurrence["code"] == "xml-comment-dropped"
    assert result.migration_report_sha256 is not None
    assert result.migration_report_sha256.startswith("sha256:")


def test_current_page_conversion_accepts_an_authenticated_table_attachment() -> None:
    storage = (
        "<table><tbody><tr><td><p><ac:link><ri:attachment "
        'ri:filename="asset-01.png"/></ac:link></p></td></tr></tbody></table>'
    )

    result = confluence_storage_to_md_result(storage, profile="editable")

    assert result.push_safe
    assert result.losses == ()


def test_portable_pull_rejects_unrelated_file_before_remote_read(tmp_path: Path) -> None:
    output = tmp_path / "portable.md"
    output.write_text("user-owned\n", encoding="utf-8")
    client = FakeClient()

    with pytest.raises(ConflictError) as error:
        pull_md(client, "123456", output_path=output, portable=True)

    assert error.value.context["reason"] == "output_conflict"
    assert client.calls == 0
    assert output.read_text(encoding="utf-8") == "user-owned\n"


def test_portable_repull_rejects_local_content_edit(tmp_path: Path) -> None:
    output = tmp_path / "portable.md"
    client = FakeClient()
    pull_md(client, "123456", output_path=output, portable=True)
    original_calls = client.calls
    output.write_text(output.read_text(encoding="utf-8") + "local edit\n", encoding="utf-8")

    with pytest.raises(ConflictError) as error:
        pull_md(client, "123456", output_path=output, portable=True)

    assert error.value.context["reason"] == "local_changes"
    assert client.calls == original_calls


def test_portable_pull_defaults_to_stem_assets_and_embeds_identity(tmp_path: Path) -> None:
    output = tmp_path / "page.md"

    pull_md(FakeAssetClient(), "123456", output_path=output, portable=True)

    managed = output.read_text(encoding="utf-8")
    parsed = parse_managed_document(managed, assets=extract_asset_records(managed))
    [asset] = parsed.assets
    assert asset.materialization == "local"
    assert asset.remote_id == "att-1"
    assert asset.remote_version == 4
    assert asset.remote_name == "road map#v1?100%-表.png"
    assert asset.src.startswith("page.assets/")
    assert (tmp_path / asset.src).read_bytes() == b"synthetic-image"


def test_portable_no_assets_keeps_remote_identity_and_hash_without_materialization(tmp_path: Path) -> None:
    output = tmp_path / "page.md"

    pull_md(FakeAssetClient(), "123456", output_path=output, portable=True, no_assets=True)

    managed = output.read_text(encoding="utf-8")
    parsed = parse_managed_document(managed, assets=extract_asset_records(managed))
    [asset] = parsed.assets
    assert asset.materialization == "remote-only"
    assert asset.remote_id == "att-1"
    assert asset.sha256.startswith("sha256:")
    assert not (tmp_path / "page.assets").exists()


def test_portable_pull_records_opaque_attachment_without_requiring_markdown_image(tmp_path: Path) -> None:
    output = tmp_path / "page.md"

    pull_md(FakeOpaqueAssetClient(), "123456", output_path=output, portable=True)

    managed = output.read_text(encoding="utf-8")
    parsed = parse_managed_document(managed, assets=extract_asset_records(managed))
    [asset] = parsed.assets
    assert asset.materialization == "local"
    assert asset.remote_id == "att-1"
    assert asset.remote_name == "road map#v1?100%-表.png"
    assert (tmp_path / asset.src).read_bytes() == b"synthetic-image"


def test_detached_asset_path_is_page_bound_not_reused_from_a_sibling(tmp_path: Path) -> None:
    unrelated = tmp_path / "unrelated.assets"
    unrelated.mkdir()
    (unrelated / "road-map-v1-100.png").write_bytes(b"synthetic-image")

    first = tmp_path / "page.md"
    second = tmp_path / "repull.md"
    pull_md(FakeDetachedAssetClient(), "123456", output_path=first, portable=True)
    pull_md(FakeDetachedAssetClient(), "123456", output_path=second, portable=True)

    first_asset = parse_managed_document(
        first.read_text(encoding="utf-8"), assets=extract_asset_records(first.read_text(encoding="utf-8"))
    ).assets[0]
    second_asset = parse_managed_document(
        second.read_text(encoding="utf-8"), assets=extract_asset_records(second.read_text(encoding="utf-8"))
    ).assets[0]
    assert first_asset.src == second_asset.src
    assert first_asset.src.startswith(".atls-detached-")
    assert first_asset.src != "unrelated.assets/road-map-v1-100.png"
    assert (tmp_path / first_asset.src).read_bytes() == b"synthetic-image"


def test_byte_identical_portable_repull_preserves_file_identity_and_mtime(tmp_path: Path) -> None:
    output = tmp_path / "portable.md"
    client = FakeClient()
    pull_md(client, "123456", output_path=output, portable=True, no_assets=True)
    before = output.stat()

    pull_md(client, "123456", output_path=output, portable=True, no_assets=True)

    after = output.stat()
    assert (after.st_dev, after.st_ino, after.st_mtime_ns) == (before.st_dev, before.st_ino, before.st_mtime_ns)
