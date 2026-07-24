from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from atlassian_skills.confluence.asset_sync import (
    AssetAction,
    AssetBaseline,
    ManagedAssetReference,
    RemoteAttachmentSnapshot,
    build_asset_plan,
    extract_managed_asset_references,
    rewrite_attachment_markdown,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _baseline(path: Path, *, content: bytes = b"old", version: int = 4) -> AssetBaseline:
    return AssetBaseline(
        attachment_id="att-1",
        attachment_version=version,
        filename=path.name,
        local_path=path,
        content_sha256=_sha(content),
        media_type="image/png",
        reference_state="referenced_local",
    )


def _remote(path: Path, *, content: bytes = b"old", version: int = 4) -> RemoteAttachmentSnapshot:
    return RemoteAttachmentSnapshot(
        attachment_id="att-1",
        version=version,
        filename=path.name,
        content_sha256=_sha(content),
        size=len(content),
        media_type="image/png",
    )


def test_canonical_image_metadata_comment_survives_attachment_rewrite() -> None:
    markdown = (
        "![diagram](remote.png)"
        "<!-- cfxmark:img w=320 h=180 thumbnail=1 align=center -->"
        '<!-- cfxmark:asset src="remote.png" -->'
    )

    rewritten = rewrite_attachment_markdown(markdown, {"remote.png": "assets/local.png"})

    assert rewritten == ("![diagram](assets/local.png)<!-- cfxmark:img w=320 h=180 thumbnail=1 align=center -->")


def test_canonical_image_metadata_comment_does_not_hide_remote_asset_identity() -> None:
    markdown = (
        "![diagram](assets/local.png)"
        "<!-- cfxmark:img w=320 h=180 thumbnail=1 align=center -->"
        '<!-- cfxmark:asset src="remote.png" -->'
    )

    assert extract_managed_asset_references(markdown) == (
        ManagedAssetReference(local_reference="assets/local.png", remote_filename="remote.png"),
    )


def test_absolute_image_with_query_is_not_a_managed_local_asset() -> None:
    markdown = "![diagram](https://example.com/download/image.png?version=1&api=v2)"

    assert extract_managed_asset_references(markdown) == ()


def test_relative_local_image_query_is_rejected() -> None:
    with pytest.raises(ValueError, match="query or fragment"):
        extract_managed_asset_references("![diagram](assets/image.png?version=1)")


def test_smart_plan_uses_identity_version_and_hash_for_unchanged(tmp_path: Path) -> None:
    managed = tmp_path / "page.md"
    asset = tmp_path / "assets" / "diagram.png"
    asset.parent.mkdir()
    asset.write_bytes(b"old")

    plan = build_asset_plan(
        managed_path=managed,
        references=("assets/diagram.png",),
        baselines=(_baseline(asset),),
        remote=(_remote(asset),),
    )

    assert plan.dirty is False
    assert plan.uploads == ()
    assert plan.items[0].action is AssetAction.UNCHANGED
    assert plan.remote_deletes == ()


def test_smart_plan_separates_modified_asset_from_body_state(tmp_path: Path) -> None:
    managed = tmp_path / "page.md"
    asset = tmp_path / "assets" / "diagram.png"
    asset.parent.mkdir()
    asset.write_bytes(b"new")

    plan = build_asset_plan(
        managed_path=managed,
        references=("assets/diagram.png",),
        baselines=(_baseline(asset),),
        remote=(_remote(asset),),
    )

    assert plan.dirty is True
    assert [item.action for item in plan.uploads] == [AssetAction.NEW_VERSION]
    assert plan.conflicts == ()


def test_remote_version_or_hash_drift_is_conflict_before_upload(tmp_path: Path) -> None:
    managed = tmp_path / "page.md"
    asset = tmp_path / "assets" / "diagram.png"
    asset.parent.mkdir()
    asset.write_bytes(b"new")

    plan = build_asset_plan(
        managed_path=managed,
        references=("assets/diagram.png",),
        baselines=(_baseline(asset),),
        remote=(_remote(asset, content=b"remote drift", version=5),),
    )

    assert plan.uploads == ()
    assert len(plan.conflicts) == 1
    assert plan.conflicts[0].reason == "remote_attachment_drift"


def test_new_reference_does_not_overwrite_same_filename_different_content(tmp_path: Path) -> None:
    managed = tmp_path / "page.md"
    asset = tmp_path / "assets" / "diagram.png"
    asset.parent.mkdir()
    asset.write_bytes(b"new")

    plan = build_asset_plan(
        managed_path=managed,
        references=("assets/diagram.png",),
        baselines=(),
        remote=(_remote(asset, content=b"other"),),
    )

    assert len(plan.conflicts) == 1
    assert plan.conflicts[0].reason == "same_filename_different_content"


def test_unreferenced_baseline_is_reported_but_never_deleted_or_uploaded(tmp_path: Path) -> None:
    managed = tmp_path / "page.md"
    asset = tmp_path / "assets" / "unused.png"
    asset.parent.mkdir()
    asset.write_bytes(b"old")

    plan = build_asset_plan(
        managed_path=managed,
        references=(),
        baselines=(_baseline(asset),),
        remote=(_remote(asset),),
    )

    assert [item.action for item in plan.unreferenced] == [AssetAction.UNREFERENCED]
    assert plan.uploads == ()
    assert plan.remote_deletes == ()
    assert plan.dirty is False


def test_percent_decoded_special_filename_is_a_physical_path_not_url_components(tmp_path: Path) -> None:
    managed = tmp_path / "page.md"
    asset = tmp_path / "assets" / "road map#v1?100%-表.png"
    asset.parent.mkdir()
    asset.write_bytes(b"new")

    plan = build_asset_plan(
        managed_path=managed,
        references=("assets/road map#v1?100%-表.png",),
        baselines=(),
        remote=(),
    )

    assert len(plan.uploads) == 1
    assert plan.uploads[0].action is AssetAction.NEW
    assert plan.uploads[0].local_path == asset.resolve()
    assert plan.uploads[0].remote_filename == asset.name


def test_cfxmark_v3_asset_marker_rewrite_decodes_exact_remote_identity() -> None:
    remote_name = "road map#v1?100%-表.png"
    encoded = remote_name.encode("utf-8").hex()
    markdown = f'![](<{remote_name}>)<!-- cfxmark:asset v=3 src="{encoded}" -->\n'

    rewritten = rewrite_attachment_markdown(markdown, {remote_name: "page.assets/local.png"})

    assert rewritten == "![](page.assets/local.png)\n"
    assert "cfxmark:asset" not in rewritten
