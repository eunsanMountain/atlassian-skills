from __future__ import annotations

from pathlib import Path

import pytest

from atlassian_skills.core.managed_manifest import (
    ManagedAssetRecord,
    ManagedManifest,
    ManagedManifestError,
    canonical_asset_set_sha256,
    canonical_content_sha256,
    parse_managed_document,
    parse_managed_manifest,
    parse_passthrough,
    serialize_managed_manifest,
    serialize_passthrough,
    strip_managed_manifest,
)

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _manifest(*, base_md: str = HASH_B, assets: str = HASH_C) -> ManagedManifest:
    return ManagedManifest(
        v=2,
        page="123",
        site=HASH_A,
        remote_version=11,
        remote_storage=HASH_A,
        base_md=base_md,
        assets=assets,
        converter="cfxmark/0.5.0",
        profile="markdown-first",
        passthrough=("ac:", "x,y", "한글:"),
    )


def test_manifest_has_exact_top_marker_serialization_and_round_trip() -> None:
    manifest = _manifest()

    marker = serialize_managed_manifest(manifest)

    assert marker == (
        "<!-- atls:managed v=2 page=123 site="
        + HASH_A
        + " remote_version=11 remote_storage="
        + HASH_A
        + " base_md="
        + HASH_B
        + " assets="
        + HASH_C
        + " converter=cfxmark/0.5.0 profile=markdown-first "
        "passthrough=ac%3A,x%2Cy,%ED%95%9C%EA%B8%80%3A -->"
    )
    assert parse_managed_manifest(marker + "\n# Body\n") == manifest
    assert strip_managed_manifest(marker + "\n# Body\n") == ("# Body\n", manifest)


def test_manifest_is_path_independent_when_file_is_copied_or_moved(tmp_path: Path) -> None:
    body = "# Portable\n"
    manifest = _manifest(base_md=canonical_content_sha256(body), assets=canonical_asset_set_sha256(()))
    managed = serialize_managed_manifest(manifest) + "\n" + body
    first = tmp_path / "first.md"
    moved = tmp_path / "nested" / "renamed.md"
    first.write_text(managed, encoding="utf-8")
    moved.parent.mkdir()
    moved.write_bytes(first.read_bytes())

    assert parse_managed_document(first.read_text(encoding="utf-8")).manifest == manifest
    assert parse_managed_document(moved.read_text(encoding="utf-8")).manifest == manifest


def test_canonical_content_hash_normalizes_newlines_and_strips_only_control_comments() -> None:
    left = (
        "\ufeff<!-- atls:managed ignored -->\r\n"
        "<!-- cfxmark:notice guidance -->\r\n"
        "# Body\r\n"
        "text<!-- cfxmark:migration id=mig_1 -->\r\n"
        "![x](a.png)<!-- cfxmark:img w=320 --><!-- cfxmark:asset src=a.png -->\r\n"
    )
    right = "# Body\ntext\n![x](a.png)<!-- cfxmark:img w=320 -->\n"

    assert canonical_content_sha256(left) == canonical_content_sha256(right)


def test_asset_set_hash_is_order_independent_and_includes_remote_only_authority() -> None:
    local = ManagedAssetRecord("local", "page.assets/a.png", "10", 2, "a.png", HASH_A)
    remote_only = ManagedAssetRecord("remote-only", "page.assets/b.png", "11", 4, "b.png", HASH_B)

    assert canonical_asset_set_sha256((local, remote_only)) == canonical_asset_set_sha256((remote_only, local))
    assert canonical_asset_set_sha256((local,)) != canonical_asset_set_sha256((local, remote_only))


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        ("", "invalid_passthrough_prefix"),
        ("atls:owned", "reserved_passthrough_prefix"),
        ("CFXMARK:owned", "reserved_passthrough_prefix"),
        ("x--y", "invalid_passthrough_prefix"),
        ("x>y", "invalid_passthrough_prefix"),
        ("x\u202ey", "invalid_passthrough_prefix"),
    ],
)
def test_passthrough_rejects_reserved_or_comment_unsafe_tokens(token: str, reason: str) -> None:
    with pytest.raises(ManagedManifestError, match=reason):
        serialize_passthrough((token,))


def test_passthrough_is_nfc_sorted_percent_encoded_and_canonical() -> None:
    serialized = serialize_passthrough(("한글:", "x,y", "ac:", "ac:", "e\u0301:"))

    assert serialized == "ac%3A,x%2Cy,%C3%A9%3A,%ED%95%9C%EA%B8%80%3A"
    assert parse_passthrough(serialized) == ("ac:", "x,y", "é:", "한글:")
    with pytest.raises(ManagedManifestError, match="noncanonical_passthrough"):
        parse_passthrough("x%2cy")
    with pytest.raises(ManagedManifestError, match="invalid_percent_encoding"):
        parse_passthrough("x%ZZy")


@pytest.mark.parametrize(
    ("managed", "reason"),
    [
        ("# Body\n", "missing_managed_manifest"),
        ("# Before\n<!-- atls:managed v=2 -->\n", "managed_manifest_not_first"),
        (
            "```md\n<!-- atls:managed v=2 page=123 -->\n```\n",
            "missing_managed_manifest",
        ),
        (
            '<!-- atls:binding {"v":1} -->\n# Body\n',
            "legacy_binding_marker",
        ),
    ],
)
def test_missing_fenced_misplaced_and_old_v1_markers_fail_closed(managed: str, reason: str) -> None:
    with pytest.raises(ManagedManifestError, match=reason):
        parse_managed_manifest(managed)


def test_duplicate_manifest_fails_closed() -> None:
    marker = serialize_managed_manifest(_manifest())

    with pytest.raises(ManagedManifestError, match="duplicate_managed_manifest"):
        parse_managed_manifest(marker + "\n" + marker + "\n")


def test_unknown_duplicate_or_out_of_order_fields_fail_closed() -> None:
    marker = serialize_managed_manifest(_manifest())

    for corrupt in (
        marker.replace(" page=123", " page=123 page=123"),
        marker.replace(" page=123", " unknown=x page=123"),
        marker.replace(" v=2 page=123", " page=123 v=2"),
    ):
        with pytest.raises(ManagedManifestError):
            parse_managed_manifest(corrupt + "\n")


def test_content_or_asset_tamper_is_structured_and_fail_closed() -> None:
    body = "# Body\n"
    asset = ManagedAssetRecord("local", "page.assets/a.png", "10", 2, "a.png", HASH_A)
    manifest = _manifest(
        base_md=canonical_content_sha256(body),
        assets=canonical_asset_set_sha256((asset,)),
    )
    managed = serialize_managed_manifest(manifest) + "\n" + body

    parsed = parse_managed_document(managed, assets=(asset,))
    assert parsed.content == body

    with pytest.raises(ManagedManifestError, match="managed_content_tampered") as content_error:
        parse_managed_document(managed + "changed\n", assets=(asset,))
    assert content_error.value.context["expected"] == manifest.base_md

    with pytest.raises(ManagedManifestError, match="managed_assets_tampered") as asset_error:
        parse_managed_document(managed, assets=())
    assert asset_error.value.context["expected"] == manifest.assets
