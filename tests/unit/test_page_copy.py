from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO

import pytest

from atlassian_skills.confluence.models import Attachment, PageLinks
from atlassian_skills.confluence.page_copy import copy_page
from atlassian_skills.core.errors import AtlasError, ConflictError, ValidationError


class FakeCopyClient:
    source_id = "source-1"
    parent_id = "parent-1"
    target_id = "target-1"
    source_storage = '<p><ac:image><ri:attachment ri:filename="a*b.png" /></ac:image></p>'
    attachment_bytes = b"exact attachment bytes"

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.uploaded: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.comments: list[tuple[str, str]] = []
        self.source_reads = 0
        self.fail_upload = False
        self.fail_delete = False
        self.drift_source = False
        self.source_attachment_count = 1
        self.target_attachment_bytes = self.attachment_bytes
        self.create_response_id = self.target_id
        self.lose_create_response = False
        self.lose_create_before_record = False
        self.hide_created_from_search = False
        self.preexisting_title_ids: tuple[str, ...] = ()
        self.preexisting_title = "Source title"
        self.target_title_override: str | None = None
        self.replace_staged_path_with: Path | None = None

    def get_page_raw(self, page_id: str, expand: str = "") -> dict[str, Any]:
        del expand
        if page_id == self.source_id:
            self.source_reads += 1
            storage = self.source_storage
            version = 7
            if self.drift_source and self.source_reads > 1:
                storage = "<p>concurrent edit</p>"
                version = 8
            return {
                "id": self.source_id,
                "title": "Source title",
                "status": "current",
                "space": {"key": "SRC"},
                "version": {"number": version},
                "body": {"storage": {"value": storage, "representation": "storage"}},
            }
        if page_id == self.parent_id:
            return {
                "id": self.parent_id,
                "title": "Destination parent",
                "status": "current",
                "space": {"key": "DST"},
                "version": {"number": 3},
            }
        if page_id == self.target_id:
            requested_title = self.created[-1]["title"] if self.created else "Copied title"
            return {
                "id": self.target_id,
                "title": self.target_title_override or requested_title,
                "status": "current",
                "space": {"key": "DST"},
                "version": {"number": 1},
                "ancestors": [{"id": self.parent_id}],
                "body": {"storage": {"value": self.source_storage, "representation": "storage"}},
            }
        raise AssertionError(f"unexpected page id: {page_id}")

    def list_attachments(self, page_id: str, limit: int = 50) -> list[Attachment]:
        del limit
        if page_id == self.source_id:
            return [
                Attachment(
                    id=f"att-source-{index}",
                    title="a*b.png" if index == 0 else f"asset-{index}.bin",
                    version=4,
                    file_size=len(self.attachment_bytes),
                    media_type="image/png",
                    links=PageLinks(download="/download/source"),
                )
                for index in range(self.source_attachment_count)
            ]
        if page_id == self.target_id and self.uploaded:
            return [
                Attachment(
                    id="att-target",
                    title="a*b.png",
                    version=1,
                    file_size=len(self.attachment_bytes),
                    media_type="image/png",
                    links=PageLinks(download="/download/target"),
                )
            ]
        return []

    def fetch_attachment_bytes(self, att_id: str, download_link: str | None = None) -> bytes:
        del download_link
        if att_id.startswith("att-source"):
            return self.attachment_bytes
        if att_id == "att-target":
            return self.target_attachment_bytes
        raise AssertionError(f"unexpected attachment id: {att_id}")

    def search(self, cql: str, limit: int = 25) -> SimpleNamespace:
        del cql, limit
        if self.preexisting_title_ids:
            return SimpleNamespace(
                results=[
                    SimpleNamespace(id=page_id, title=self.preexisting_title) for page_id in self.preexisting_title_ids
                ]
            )
        if not self.created:
            return SimpleNamespace(results=[])
        if self.hide_created_from_search:
            return SimpleNamespace(results=[])
        return SimpleNamespace(results=[SimpleNamespace(id=self.target_id, title=self.created[-1]["title"])])

    def create_page(
        self,
        space_key: str,
        title: str,
        body: str,
        ancestor_id: str | None = None,
        body_format: str = "storage",
    ) -> dict[str, Any]:
        if self.lose_create_before_record:
            raise AtlasError("synthetic create failure before remote mutation")
        self.created.append(
            {
                "space_key": space_key,
                "title": title,
                "body": body,
                "ancestor_id": ancestor_id,
                "body_format": body_format,
            }
        )
        if self.lose_create_response:
            raise AtlasError("synthetic create response loss")
        return {"id": self.create_response_id, "title": title, "version": {"number": 1}, "space": {"key": space_key}}

    def upload_attachment(
        self,
        page_id: str,
        file_path: str | Path,
        comment: str | None = None,
        *,
        filename: str | None = None,
        source_stream: BinaryIO | None = None,
    ) -> dict[str, Any]:
        if self.fail_upload:
            raise ValidationError("upload failed", context={"reason": "test_upload_failure"})
        path = Path(file_path)
        if self.replace_staged_path_with is not None:
            path.unlink()
            path.symlink_to(self.replace_staged_path_with)
        content = source_stream.read() if source_stream is not None else path.read_bytes()
        self.uploaded.append(
            {
                "page_id": page_id,
                "filename": filename,
                "bytes": content,
                "comment": comment,
            }
        )
        return {"results": [{"id": "att-target", "title": filename, "version": {"number": 1}}], "size": 1}

    def get_children(self, page_id: str, limit: int = 25) -> list[Any]:
        del limit
        assert page_id == self.parent_id
        return [SimpleNamespace(id=self.target_id)] if self.created else []

    def add_comment(self, page_id: str, body: str) -> dict[str, Any]:
        self.comments.append((page_id, body))
        return {"id": "comment-1"}

    def delete_page(self, page_id: str) -> None:
        if self.fail_delete:
            raise ValidationError("delete failed", context={"reason": "test_delete_failure"})
        self.deleted.append(page_id)


def test_copy_page_dry_run_preflights_without_writes() -> None:
    client = FakeCopyClient()

    result = copy_page(
        client,
        client.source_id,
        destination_parent_id=client.parent_id,
        destination_space="DST",
        include_attachments=True,
        verify=True,
        dry_run=True,
    )

    assert result["status"] == "dry_run"
    assert result["attachments"] == {"count": 1, "total_bytes": len(client.attachment_bytes)}
    assert client.created == []
    assert client.uploaded == []
    assert client.deleted == []


def test_copy_page_requires_explicit_attachment_copy() -> None:
    client = FakeCopyClient()

    with pytest.raises(ValidationError) as exc_info:
        copy_page(
            client,
            client.source_id,
            destination_parent_id=client.parent_id,
            destination_space="DST",
            include_attachments=False,
        )

    assert exc_info.value.context["reason"] == "attachments_not_included"
    assert client.created == []


def test_copy_page_preserves_storage_attachment_title_and_bytes() -> None:
    client = FakeCopyClient()

    result = copy_page(
        client,
        client.source_id,
        destination_parent_id=client.parent_id,
        destination_space="DST",
        title="Copied title",
        include_attachments=True,
        verify=True,
        reason="Validation <reason>",
    )

    assert result["status"] == "copied"
    assert result["target"]["id"] == client.target_id
    assert result["target"]["version"] == 1
    assert result["storage"]["sha256_equal"] is True
    assert result["attachments"]["verified"] == 1
    assert result["source_unchanged"] is True
    assert client.created == [
        {
            "space_key": "DST",
            "title": "Copied title",
            "body": client.source_storage,
            "ancestor_id": client.parent_id,
            "body_format": "storage",
        }
    ]
    assert client.uploaded == [
        {
            "page_id": client.target_id,
            "filename": "a*b.png",
            "bytes": client.attachment_bytes,
            "comment": "Copied by atls page copy",
        }
    ]
    assert client.comments == [(client.target_id, "<p>Validation &lt;reason&gt;</p>")]
    assert client.deleted == []


def test_copy_page_accepts_verified_create_response_before_search_index_catches_up() -> None:
    client = FakeCopyClient()
    client.hide_created_from_search = True

    result = copy_page(
        client,
        client.source_id,
        destination_parent_id=client.parent_id,
        destination_space="DST",
        title="Copied title",
        include_attachments=True,
        verify=True,
    )

    assert result["status"] == "copied"
    assert result["create_outcome"] == "response"
    assert result["target"]["id"] == client.target_id
    assert result["storage"]["sha256_equal"] is True
    assert result["attachments"]["verified"] == 1
    assert client.deleted == []


def test_copy_page_blocks_parent_space_mismatch_before_write() -> None:
    client = FakeCopyClient()

    with pytest.raises(ValidationError) as exc_info:
        copy_page(
            client,
            client.source_id,
            destination_parent_id=client.parent_id,
            destination_space="OTHER",
            include_attachments=True,
        )

    assert exc_info.value.context["reason"] == "destination_parent_space_mismatch"
    assert client.created == []


def test_copy_page_blocks_source_drift_before_create() -> None:
    client = FakeCopyClient()
    client.drift_source = True

    with pytest.raises(ValidationError) as exc_info:
        copy_page(
            client,
            client.source_id,
            destination_parent_id=client.parent_id,
            destination_space="DST",
            include_attachments=True,
        )

    assert exc_info.value.context["reason"] == "source_changed_during_copy"
    assert client.created == []


def test_copy_page_rolls_back_destination_after_upload_failure() -> None:
    client = FakeCopyClient()
    client.fail_upload = True

    with pytest.raises(ValidationError) as exc_info:
        copy_page(
            client,
            client.source_id,
            destination_parent_id=client.parent_id,
            destination_space="DST",
            include_attachments=True,
        )

    assert exc_info.value.context["reason"] == "test_upload_failure"
    assert client.deleted == [client.target_id]


def test_copy_page_rolls_back_destination_after_attachment_hash_mismatch() -> None:
    client = FakeCopyClient()
    client.target_attachment_bytes = b"server returned different bytes"

    with pytest.raises(ValidationError) as exc_info:
        copy_page(
            client,
            client.source_id,
            destination_parent_id=client.parent_id,
            destination_space="DST",
            include_attachments=True,
            verify=True,
        )

    assert exc_info.value.context["reason"] == "copied_attachment_hash_mismatch"
    assert client.deleted == [client.target_id]


def test_copy_page_reports_target_when_rollback_fails() -> None:
    client = FakeCopyClient()
    client.fail_upload = True
    client.fail_delete = True

    with pytest.raises(ValidationError) as exc_info:
        copy_page(
            client,
            client.source_id,
            destination_parent_id=client.parent_id,
            destination_space="DST",
            include_attachments=True,
        )

    assert exc_info.value.context == {
        "reason": "page_copy_cleanup_failed",
        "target_page_id": client.target_id,
        "original_error": "ValidationError",
        "cleanup_error": "ValidationError",
    }
    assert client.comments == [
        (
            client.target_id,
            "<p>atls page copy failed and automatic cleanup also failed. Manual cleanup is required.</p>",
        )
    ]


def test_copy_page_rejects_attachment_count_over_bound_before_create() -> None:
    client = FakeCopyClient()
    client.source_attachment_count = 2

    with pytest.raises(ValidationError) as exc_info:
        copy_page(
            client,
            client.source_id,
            destination_parent_id=client.parent_id,
            destination_space="DST",
            include_attachments=True,
            attachment_limit=1,
        )

    assert exc_info.value.context["reason"] == "copy_attachment_count_limit_exceeded"
    assert client.created == []


def test_copy_page_without_attachments_does_not_require_include_flag() -> None:
    client = FakeCopyClient()
    client.source_attachment_count = 0
    client.source_storage = "<p>text only</p>"

    result = copy_page(
        client,
        client.source_id,
        destination_parent_id=client.parent_id,
        destination_space="DST",
        include_attachments=False,
        verify=True,
    )

    assert result["status"] == "copied"
    assert result["attachments"] == {"copied": 0, "verified": 0, "items": []}
    assert client.uploaded == []


def test_copy_page_no_verify_still_checks_attachment_identity_without_downloading_target() -> None:
    client = FakeCopyClient()
    client.target_attachment_bytes = b"would fail byte verification"

    result = copy_page(
        client,
        client.source_id,
        destination_parent_id=client.parent_id,
        destination_space="DST",
        include_attachments=True,
        verify=False,
    )

    assert result["status"] == "copied"
    assert result["attachments"]["verified"] == 0
    assert result["attachments"]["items"][0]["sha256"] is None
    assert result["attachments"]["items"][0]["sha256_equal"] is None


def test_copy_page_never_uploads_to_or_deletes_source_when_create_response_reuses_source_id() -> None:
    client = FakeCopyClient()
    client.create_response_id = client.source_id

    result = copy_page(
        client,
        client.source_id,
        destination_parent_id=client.parent_id,
        destination_space="DST",
        title="Copied title",
        include_attachments=True,
    )

    assert result["target"]["id"] == client.target_id
    assert result["create_outcome"] == "reconciled"
    assert {item["page_id"] for item in client.uploaded} == {client.target_id}
    assert client.source_id not in client.deleted


def test_copy_page_adopts_one_exact_target_after_create_response_loss() -> None:
    client = FakeCopyClient()
    client.lose_create_response = True

    result = copy_page(
        client,
        client.source_id,
        destination_parent_id=client.parent_id,
        destination_space="DST",
        title="Copied title",
        include_attachments=True,
    )

    assert result["status"] == "copied"
    assert result["create_outcome"] == "reconciled"
    assert result["target"]["id"] == client.target_id
    assert client.deleted == []


def test_copy_page_reports_unknown_create_outcome_without_guessing_cleanup() -> None:
    client = FakeCopyClient()
    client.lose_create_before_record = True

    with pytest.raises(ValidationError) as exc_info:
        copy_page(
            client,
            client.source_id,
            destination_parent_id=client.parent_id,
            destination_space="DST",
            title="Copied title",
            include_attachments=True,
        )

    assert exc_info.value.context["reason"] == "page_copy_create_outcome_unknown"
    assert exc_info.value.context["create_outcome"] == "unknown"
    assert client.uploaded == []
    assert client.deleted == []


def test_copy_page_rejects_duplicate_destination_title_during_dry_run() -> None:
    client = FakeCopyClient()
    client.preexisting_title_ids = ("existing-1",)

    with pytest.raises(ConflictError) as exc_info:
        copy_page(
            client,
            client.source_id,
            destination_parent_id=client.parent_id,
            destination_space="DST",
            include_attachments=True,
            dry_run=True,
        )

    assert exc_info.value.context["reason"] == "copy_destination_title_exists"
    assert client.created == []


def test_copy_page_rejects_server_title_normalization_before_attachment_upload() -> None:
    client = FakeCopyClient()
    client.target_title_override = "Server-normalized title"

    with pytest.raises(ValidationError) as exc_info:
        copy_page(
            client,
            client.source_id,
            destination_parent_id=client.parent_id,
            destination_space="DST",
            title="Copied title",
            include_attachments=True,
        )

    assert exc_info.value.context["reason"] == "copied_page_readback_mismatch"
    assert exc_info.value.context["expected_title"] == "Copied title"
    assert exc_info.value.context["observed_title"] == "Server-normalized title"
    assert client.uploaded == []
    assert client.deleted == []


def test_copy_page_upload_uses_opened_staged_inode_when_leaf_is_replaced(tmp_path: Path) -> None:
    client = FakeCopyClient()
    attacker_file = tmp_path / "attacker.bin"
    attacker_file.write_bytes(b"attacker-controlled bytes")
    client.replace_staged_path_with = attacker_file

    result = copy_page(
        client,
        client.source_id,
        destination_parent_id=client.parent_id,
        destination_space="DST",
        title="Copied title",
        include_attachments=True,
    )

    assert result["status"] == "copied"
    assert client.uploaded[0]["bytes"] == client.attachment_bytes
    assert attacker_file.read_bytes() == b"attacker-controlled bytes"
