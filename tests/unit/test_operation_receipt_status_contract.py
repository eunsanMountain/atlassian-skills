from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO

import cfxmark
import pytest

from atlassian_skills.confluence.managed_operation import parse_managed_operation
from atlassian_skills.confluence.pull_md import pull_md
from atlassian_skills.confluence.push_md import push_md
from atlassian_skills.core.errors import StaleError, ValidationError


class ProcessDeath(BaseException):
    pass


class RecoveryClient:
    base_url = "https://example.com/confluence"

    def __init__(self) -> None:
        self.storage = (
            '<p>A</p><p><ac:image xmlns:ac="http://atlassian.com/content" '
            'xmlns:ri="http://atlassian.com/resource/identifier"><ri:attachment '
            'ri:filename="diagram.png"/></ac:image></p>'
        )
        self.version = 7
        self.asset_id = "att-1"
        self.asset_version = 4
        self.asset_bytes = b"old"
        self.created_id: str | None = None
        self.created_name: str | None = None
        self.created_bytes: bytes | None = None
        self.created_version = 1
        self.duplicate_created = False
        self.body_fault: str | None = None
        self.upload_fault: str | None = None
        self.hide_assets_after_put = False
        self.page_get_calls = 0
        self.list_calls = 0
        self.put_calls = 0
        self.upload_calls = 0
        self.remote_mutations = 0
        self.on_page_get: tuple[int, Callable[[], None]] | None = None

    def reset_observation_counts(self) -> None:
        self.page_get_calls = 0
        self.list_calls = 0
        self.put_calls = 0
        self.upload_calls = 0
        self.remote_mutations = 0

    def get_page(self, page_id: str) -> SimpleNamespace:
        self.page_get_calls += 1
        if self.on_page_get is not None and self.page_get_calls == self.on_page_get[0]:
            self.on_page_get[1]()
        return SimpleNamespace(
            id=page_id,
            title="Recovery Matrix",
            body_storage=self.storage,
            version=SimpleNamespace(number=self.version),
        )

    def update_page(
        self,
        *,
        page_id: str,
        title: str,
        body: str,
        version_number: int,
        **_kwargs: object,
    ) -> dict[str, object]:
        self.put_calls += 1
        assert page_id == "123456"
        assert title == "Recovery Matrix"
        assert version_number == self.version + 1
        if self.body_fault == "process_before_apply":
            raise ProcessDeath()
        if self.body_fault == "loss_before_apply":
            raise RuntimeError("body response lost before apply")
        source = self.storage
        if self.body_fault in {"wrong_storage_loss", "normalize_prefix", "normalize_suffix", "normalize_full"}:
            if self.body_fault == "normalize_prefix":
                suffix = body[len(source) :]
                self.storage = source.replace("<p>A</p>", "<p>A </p>", 1) + suffix
            elif self.body_fault == "normalize_suffix":
                suffix = body[len(source) :]
                self.storage = source + suffix.replace("<p>Added</p>", "<p><span>Added</span></p>", 1)
            elif self.body_fault == "normalize_full":
                self.storage = body.replace("<p>Changed</p>", "<p><span>Changed</span></p>", 1)
            else:
                self.storage = "<p>unrelated</p>"
        else:
            self.storage = body
        self.version = version_number
        self.remote_mutations += 1
        if self.body_fault in {"process_after_apply", "normalize_suffix", "normalize_full"}:
            raise ProcessDeath()
        if self.body_fault in {"loss_after_apply", "wrong_storage_loss"}:
            raise RuntimeError("body response lost after apply")
        return {"id": page_id, "version": {"number": version_number}}

    def list_attachments(self, page_id: str) -> list[SimpleNamespace]:
        self.list_calls += 1
        if self.hide_assets_after_put and self.version > 7:
            return []
        attachments = [
            SimpleNamespace(
                id=self.asset_id,
                title="diagram.png",
                version=SimpleNamespace(number=self.asset_version),
                media_type="image/png",
                links=SimpleNamespace(download="/download/att-1"),
            )
        ]
        if self.created_id is not None and self.created_name is not None:
            attachments.append(
                SimpleNamespace(
                    id=self.created_id,
                    title=self.created_name,
                    version=SimpleNamespace(number=self.created_version),
                    media_type="image/png",
                    links=SimpleNamespace(download=f"/download/{self.created_id}"),
                )
            )
            if self.duplicate_created:
                attachments.append(
                    SimpleNamespace(
                        id="att-new-duplicate",
                        title=self.created_name,
                        version=SimpleNamespace(number=1),
                        media_type="image/png",
                        links=SimpleNamespace(download="/download/att-new-duplicate"),
                    )
                )
        return attachments

    def fetch_attachment_bytes(self, attachment_id: str, download_link: str | None) -> bytes:
        if attachment_id == self.asset_id:
            return self.asset_bytes
        assert attachment_id in {self.created_id, "att-new-duplicate"}
        assert self.created_bytes is not None
        return self.created_bytes

    def _upload_attachment_raw(
        self,
        page_id: str,
        path: Path,
        comment: str | None = None,
        *,
        filename: str | None = None,
        attachment_id: str | None = None,
        source_stream: BinaryIO | None = None,
    ) -> dict[str, object]:
        self.upload_calls += 1
        if self.upload_fault == "process_before_apply":
            raise ProcessDeath()
        uploaded = source_stream.read() if source_stream is not None else path.read_bytes()
        remote_name = filename or path.name
        if attachment_id is not None:
            assert attachment_id == self.asset_id
            self.asset_bytes = uploaded
            self.asset_version += 1
            result_id = self.asset_id
            result_version = self.asset_version
        else:
            self.created_id = "att-new"
            self.created_name = remote_name
            self.created_bytes = uploaded
            self.created_version = 1
            result_id = self.created_id
            result_version = self.created_version
        self.remote_mutations += 1
        if self.upload_fault == "process_after_apply":
            raise ProcessDeath()
        if self.upload_fault == "loss_after_apply":
            raise RuntimeError("asset response lost after apply")
        return {
            "results": [
                {
                    "id": result_id,
                    "title": remote_name,
                    "version": {"number": result_version},
                }
            ]
        }


@dataclass(frozen=True)
class RecoveryCase:
    row: str
    body: str
    assets: str
    fault: str
    expected: str
    allowed_next_mutations: int


RECOVERY_MATRIX = (
    RecoveryCase("A1", "source", "baseline", "none", "reconciled", 2),
    RecoveryCase("A2", "source", "partial-applied", "upload-response-loss", "reconciled", 0),
    RecoveryCase("A3", "source", "ambiguous", "create-response-loss", "manual_recovery", 0),
    RecoveryCase("B1", "expected", "expected", "body-response-loss", "reconciled", 0),
    RecoveryCase("B2", "expected", "expected", "mismatched-storage", "readback_pending", 0),
    RecoveryCase("B3", "source", "all-expected", "body-put-not-applied", "reconciled", 1),
    RecoveryCase("C1", "source", "baseline", "local-body-changed", "manual_recovery_local_changed", 0),
    RecoveryCase("C2", "source", "baseline", "local-asset-changed", "manual_recovery_local_changed", 0),
    RecoveryCase("C3", "unrelated", "any", "external-remote-edit", "conflict", 0),
    RecoveryCase("D1", "expected", "expected", "local-finalize-changed", "local_finalize_conflict", 0),
    RecoveryCase("D2", "expected-append", "expected", "remote-suffix-normalized", "reconciled", 0),
    RecoveryCase("D3", "expected", "missing", "incomplete-asset-evidence", "readback_pending", 0),
    RecoveryCase("D4", "expected", "expected", "remote-full-normalized", "reconciled", 0),
    RecoveryCase("D5", "expected", "expected", "remote-full-exact", "reconciled", 0),
    RecoveryCase("D6", "expected-append", "expected", "remote-append-exact", "reconciled", 0),
)


def _checkout(
    tmp_path: Path,
    *,
    client: RecoveryClient | None = None,
    passthrough_prefixes: list[str] | None = None,
) -> tuple[RecoveryClient, Path, Path]:
    client = client or RecoveryClient()
    managed = tmp_path / "page.md"
    asset_dir = tmp_path / "page.assets"
    pull_md(
        client,
        "123456",
        output_path=managed,
        portable=True,
        asset_dir=asset_dir,
        passthrough_prefixes=passthrough_prefixes,
    )
    [asset] = asset_dir.iterdir()
    client.reset_observation_counts()
    return client, managed, asset


def _push(client: RecoveryClient, managed: Path, **kwargs: Any) -> dict[str, Any]:
    return push_md(
        client,
        "123456",
        managed.read_text(encoding="utf-8"),
        managed_path=managed,
        **kwargs,
    )


def _edit_body(managed: Path, *, append: bool = False) -> None:
    current = managed.read_text(encoding="utf-8")
    managed.write_text(current + "\nAdded\n" if append else current.replace("A", "Changed"), encoding="utf-8")


@pytest.mark.parametrize("case", RECOVERY_MATRIX, ids=lambda case: case.row)
def test_state_free_recovery_matrix(case: RecoveryCase, tmp_path: Path) -> None:
    client, managed, asset = _checkout(tmp_path)

    if case.row == "A1":
        _edit_body(managed)
        asset.write_bytes(b"new")
        result = _push(client, managed)
        assert client.remote_mutations == case.allowed_next_mutations
    elif case.row == "A2":
        asset.write_bytes(b"new")
        client.upload_fault = "process_after_apply"
        with pytest.raises(ProcessDeath):
            _push(client, managed)
        before = client.remote_mutations
        client.upload_fault = None
        result = _push(client, managed)
        assert client.remote_mutations - before == case.allowed_next_mutations
        assert client.upload_calls == 1
    elif case.row == "A3":
        new_asset = asset.parent / "new.png"
        new_asset.write_bytes(b"new")
        managed.write_text(
            managed.read_text(encoding="utf-8") + "\n![new](page.assets/new.png)\n",
            encoding="utf-8",
        )
        client.upload_fault = "process_after_apply"
        with pytest.raises(ProcessDeath):
            _push(client, managed)
        client.duplicate_created = True
        before = client.remote_mutations
        client.upload_fault = None
        result = _push(client, managed)
        assert client.remote_mutations - before == case.allowed_next_mutations
        assert client.upload_calls == 1
    elif case.row == "B1":
        _edit_body(managed)
        client.body_fault = "loss_after_apply"
        result = _push(client, managed)
        client.body_fault = None
        before = client.remote_mutations
        assert _push(client, managed)["status"] == "no_change"
        assert client.remote_mutations - before == case.allowed_next_mutations
        assert client.put_calls == 1
    elif case.row == "B2":
        _edit_body(managed)
        client.body_fault = "wrong_storage_loss"
        result = _push(client, managed)
        client.body_fault = None
        before = client.remote_mutations
        repeated = _push(client, managed)
        assert repeated["status"] == case.expected
        assert client.remote_mutations - before == case.allowed_next_mutations
        assert client.put_calls == 1
    elif case.row == "B3":
        _edit_body(managed)
        asset.write_bytes(b"new")
        client.body_fault = "process_before_apply"
        with pytest.raises(ProcessDeath):
            _push(client, managed)
        before = client.remote_mutations
        client.body_fault = None
        result = _push(client, managed)
        assert client.remote_mutations - before == case.allowed_next_mutations
        assert client.upload_calls == 1
    elif case.row == "C1":
        _edit_body(managed)
        client.body_fault = "process_before_apply"
        with pytest.raises(ProcessDeath):
            _push(client, managed)
        managed.write_text(managed.read_text(encoding="utf-8") + "local\n", encoding="utf-8")
        before = client.remote_mutations
        client.body_fault = None
        result = _push(client, managed)
        assert client.remote_mutations - before == case.allowed_next_mutations
    elif case.row == "C2":
        asset.write_bytes(b"new")
        client.upload_fault = "process_before_apply"
        with pytest.raises(ProcessDeath):
            _push(client, managed)
        asset.write_bytes(b"changed-again")
        before = client.remote_mutations
        client.upload_fault = None
        result = _push(client, managed)
        assert client.remote_mutations - before == case.allowed_next_mutations
    elif case.row == "C3":
        _edit_body(managed)
        client.body_fault = "process_before_apply"
        with pytest.raises(ProcessDeath):
            _push(client, managed)
        client.storage = "<p>external</p>"
        client.version = 8
        before = client.remote_mutations
        client.body_fault = None
        result = _push(client, managed)
        assert client.remote_mutations - before == case.allowed_next_mutations
    elif case.row == "D1":
        _edit_body(managed)
        client.on_page_get = (3, lambda: managed.write_text(managed.read_text(encoding="utf-8") + "local\n"))
        result = _push(client, managed)
        before = client.remote_mutations
        client.on_page_get = None
        assert _push(client, managed)["status"] == "manual_recovery_local_changed"
        assert client.remote_mutations - before == case.allowed_next_mutations
    elif case.row == "D2":
        _edit_body(managed, append=True)
        client.body_fault = "normalize_suffix"
        with pytest.raises(ProcessDeath):
            _push(client, managed)
        before = client.remote_mutations
        client.body_fault = None
        result = _push(client, managed)
        assert client.remote_mutations - before == case.allowed_next_mutations
    elif case.row == "D3":
        _edit_body(managed)
        asset.write_bytes(b"new")
        client.hide_assets_after_put = True
        result = _push(client, managed)
        before = client.remote_mutations
        repeated = _push(client, managed)
        assert repeated["status"] == case.expected
        assert client.remote_mutations - before == case.allowed_next_mutations
    else:
        _edit_body(managed, append=case.row == "D6")
        client.body_fault = "normalize_full" if case.row == "D4" else "process_after_apply"
        with pytest.raises(ProcessDeath):
            _push(client, managed)
        before = client.remote_mutations
        client.body_fault = None
        result = _push(client, managed)
        assert client.remote_mutations - before == case.allowed_next_mutations
        assert client.put_calls == 1

    assert result["status"] == case.expected
    if case.expected in {"manual_recovery", "readback_pending", "conflict"}:
        [action] = result["next_actions"]
        assert action["id"] in {"inspect_operation", "resume_operation"}
        assert action["requires_user_approval"] is False
        assert action["description_code"].isupper()
        assert "argv" not in action


def test_recovery_validates_site_authority_before_remote_read_or_finalize(tmp_path: Path) -> None:
    client, managed, _asset = _checkout(tmp_path)
    _edit_body(managed)
    client.body_fault = "process_after_apply"
    with pytest.raises(ProcessDeath):
        _push(client, managed)
    client.body_fault = None
    client.reset_observation_counts()
    client.base_url = "https://other.example/confluence"

    with pytest.raises(ValidationError) as exc_info:
        _push(client, managed)

    assert exc_info.value.context["reason"] == "managed_authority_mismatch"
    assert client.page_get_calls == 0
    assert client.remote_mutations == 0
    assert "atls:operation" in managed.read_text(encoding="utf-8")


def test_recovery_rejects_tampered_proof_bundle_before_remote_read(tmp_path: Path) -> None:
    client, managed, _asset = _checkout(tmp_path)
    _edit_body(managed)
    client.body_fault = "process_before_apply"
    with pytest.raises(ProcessDeath):
        _push(client, managed)
    client.body_fault = None
    marked = managed.read_text(encoding="utf-8")
    managed.write_text(
        re.sub(r"report=sha256:[0-9a-f]{64}", "report=sha256:" + "0" * 64, marked, count=1),
        encoding="utf-8",
    )
    client.reset_observation_counts()

    with pytest.raises(ValidationError) as exc_info:
        _push(client, managed)

    assert exc_info.value.context["reason"] == "invalid_operation_bundle"
    assert client.page_get_calls == 0
    assert client.remote_mutations == 0


# The manifest a pull writes, resolved at import time because parametrize is
# evaluated then. Derived, not pinned: a converter release must not need a
# test edit to keep this contract meaningful.
CURRENT_CONVERTER = f"converter=cfxmark/{cfxmark.__version__}"


@pytest.mark.parametrize(
    ("old", "new", "expected_reason"),
    [
        # `old` must be whatever the pull actually wrote, or the replacement is a
        # no-op and the test passes for the wrong reason. `new` only has to differ.
        (CURRENT_CONVERTER, "converter=cfxmark/0.0.0", "managed_converter_mismatch"),
        ("profile=markdown-first", "profile=other-profile", "managed_converter_mismatch"),
        ("passthrough=-", "passthrough=safe-prefix", "operation_authority_mismatch"),
    ],
)
def test_recovery_binds_converter_profile_and_passthrough_before_remote_read(
    tmp_path: Path,
    old: str,
    new: str,
    expected_reason: str,
) -> None:
    client, managed, _asset = _checkout(tmp_path)
    _edit_body(managed)
    client.body_fault = "process_before_apply"
    with pytest.raises(ProcessDeath):
        _push(client, managed)
    client.body_fault = None
    marked = managed.read_text(encoding="utf-8")
    assert marked.count(old) == 1
    managed.write_text(marked.replace(old, new, 1), encoding="utf-8")
    client.reset_observation_counts()

    with pytest.raises(ValidationError) as exc_info:
        _push(client, managed)

    assert exc_info.value.context["reason"] == expected_reason
    assert client.page_get_calls == 0
    assert client.remote_mutations == 0


@pytest.mark.parametrize(
    ("retry_kwargs", "expected_reason", "expected_error"),
    [
        ({"if_version": 999}, "managed_if_version_mismatch", StaleError),
        ({"passthrough_prefixes": ["different:"]}, "passthrough_mismatch", ValidationError),
    ],
)
def test_pending_recovery_validates_current_invocation_before_remote_read(
    tmp_path: Path,
    retry_kwargs: dict[str, Any],
    expected_reason: str,
    expected_error: type[Exception],
) -> None:
    client, managed, _asset = _checkout(tmp_path, passthrough_prefixes=["safe:"])
    _edit_body(managed)
    client.body_fault = "process_before_apply"
    with pytest.raises(ProcessDeath):
        _push(client, managed)
    client.body_fault = None
    client.reset_observation_counts()
    marked = managed.read_text(encoding="utf-8")

    with pytest.raises(expected_error) as exc_info:
        _push(client, managed, **retry_kwargs)

    assert exc_info.value.context["reason"] == expected_reason
    assert client.page_get_calls == 0
    assert client.list_calls == 0
    assert client.put_calls == 0
    assert client.upload_calls == 0
    assert managed.read_text(encoding="utf-8") == marked


def test_pending_dry_run_reports_recovery_without_mutation_or_new_preflight(tmp_path: Path) -> None:
    client, managed, _asset = _checkout(tmp_path)
    _edit_body(managed)
    client.body_fault = "process_before_apply"
    with pytest.raises(ProcessDeath):
        _push(client, managed)
    operation_id = parse_managed_operation(managed.read_text(encoding="utf-8")).operation_id
    client.body_fault = None
    client.reset_observation_counts()
    marked = managed.read_text(encoding="utf-8")

    result = _push(client, managed, dry_run=True)

    assert result["status"] != "ready_to_publish"
    assert result["operation_id"] == operation_id
    assert result["dry_run"] is True
    assert result["would_mutate"] is True
    assert client.put_calls == 0
    assert client.upload_calls == 0
    assert client.remote_mutations == 0
    assert managed.read_text(encoding="utf-8") == marked


def test_pending_dry_run_classifies_local_asset_change_without_mutation(tmp_path: Path) -> None:
    client, managed, asset = _checkout(tmp_path)
    asset.write_bytes(b"new")
    client.upload_fault = "process_before_apply"
    with pytest.raises(ProcessDeath):
        _push(client, managed)
    asset.write_bytes(b"changed-again")
    client.upload_fault = None
    client.reset_observation_counts()
    marked = managed.read_text(encoding="utf-8")

    result = _push(client, managed, dry_run=True)

    assert result["status"] == "manual_recovery_local_changed"
    assert result["dry_run"] is True
    assert result["would_mutate"] is False
    assert client.page_get_calls == 0
    assert client.list_calls == 0
    assert client.put_calls == 0
    assert client.upload_calls == 0
    assert client.remote_mutations == 0
    assert managed.read_text(encoding="utf-8") == marked


def test_pending_dry_run_reports_journal_transition_without_applying_it(tmp_path: Path) -> None:
    client, managed, _asset = _checkout(tmp_path)
    _edit_body(managed)
    client.body_fault = "process_before_apply"
    with pytest.raises(ProcessDeath):
        _push(client, managed)
    client.body_fault = None
    client.storage = "<p>external</p>"
    client.version = 8
    client.reset_observation_counts()
    marked = managed.read_text(encoding="utf-8")

    result = _push(client, managed, dry_run=True)

    assert result["status"] == "conflict"
    assert result["dry_run"] is True
    assert result["would_mutate"] is True
    assert client.put_calls == 0
    assert client.upload_calls == 0
    assert client.remote_mutations == 0
    assert managed.read_text(encoding="utf-8") == marked


class MultiCreateRecoveryClient(RecoveryClient):
    def __init__(self) -> None:
        super().__init__()
        self.created_assets: list[tuple[str, str, bytes]] = []
        self.process_after_apply_upload: int | None = None

    def list_attachments(self, page_id: str) -> list[SimpleNamespace]:
        attachments = super().list_attachments(page_id)
        attachments.extend(
            SimpleNamespace(
                id=attachment_id,
                title=filename,
                version=SimpleNamespace(number=1),
                media_type="image/png",
                links=SimpleNamespace(download=f"/download/{attachment_id}"),
            )
            for attachment_id, filename, _content in self.created_assets
        )
        return attachments

    def fetch_attachment_bytes(self, attachment_id: str, download_link: str | None) -> bytes:
        for candidate_id, _filename, content in self.created_assets:
            if candidate_id == attachment_id:
                return content
        return super().fetch_attachment_bytes(attachment_id, download_link)

    def _upload_attachment_raw(
        self,
        page_id: str,
        path: Path,
        comment: str | None = None,
        *,
        filename: str | None = None,
        attachment_id: str | None = None,
        source_stream: BinaryIO | None = None,
    ) -> dict[str, object]:
        if attachment_id is not None:
            return super()._upload_attachment_raw(
                page_id,
                path,
                comment,
                filename=filename,
                attachment_id=attachment_id,
                source_stream=source_stream,
            )
        self.upload_calls += 1
        uploaded = source_stream.read() if source_stream is not None else path.read_bytes()
        remote_name = filename or path.name
        result_id = f"att-new-{self.upload_calls}"
        self.created_assets.append((result_id, remote_name, uploaded))
        self.remote_mutations += 1
        if self.process_after_apply_upload == self.upload_calls:
            raise ProcessDeath()
        return {
            "results": [
                {
                    "id": result_id,
                    "title": remote_name,
                    "version": {"number": 1},
                }
            ]
        }


def test_two_create_response_loss_adopts_second_receipt_without_duplicate_upload(tmp_path: Path) -> None:
    client = MultiCreateRecoveryClient()
    client, managed, asset = _checkout(tmp_path, client=client)
    for name, content in (("new-a.png", b"new-a"), ("new-b.png", b"new-b")):
        (asset.parent / name).write_bytes(content)
    managed.write_text(
        managed.read_text(encoding="utf-8") + "\n![new-a](page.assets/new-a.png)\n![new-b](page.assets/new-b.png)\n",
        encoding="utf-8",
    )
    client.process_after_apply_upload = 2

    with pytest.raises(ProcessDeath):
        _push(client, managed)
    operation_id = parse_managed_operation(managed.read_text(encoding="utf-8")).operation_id
    assert client.upload_calls == 2
    assert client.put_calls == 0

    client.process_after_apply_upload = None
    result = _push(client, managed)

    assert result["status"] == "reconciled"
    assert result["operation_id"] == operation_id
    assert result["adopted_asset_response_loss"] is True
    assert client.upload_calls == 2
    assert client.put_calls == 1
    assert "atls:operation" not in managed.read_text(encoding="utf-8")


def test_two_create_recovery_rejects_forged_predecessor_receipt_before_remote_read(tmp_path: Path) -> None:
    client = MultiCreateRecoveryClient()
    client, managed, asset = _checkout(tmp_path, client=client)
    for name, content in (("new-a.png", b"new-a"), ("new-b.png", b"new-b")):
        (asset.parent / name).write_bytes(content)
    managed.write_text(
        managed.read_text(encoding="utf-8") + "\n![new-a](page.assets/new-a.png)\n![new-b](page.assets/new-b.png)\n",
        encoding="utf-8",
    )
    client.process_after_apply_upload = 2
    with pytest.raises(ProcessDeath):
        _push(client, managed)
    client.process_after_apply_upload = None
    marked = managed.read_text(encoding="utf-8")
    assert "result_id=att-new-1" in marked
    managed.write_text(marked.replace("result_id=att-new-1", "result_id=att-forged", 1), encoding="utf-8")
    first_id, first_name, first_content = client.created_assets[0]
    assert first_id == "att-new-1"
    client.created_assets[0] = ("att-forged", first_name, first_content)
    client.reset_observation_counts()

    with pytest.raises(ValidationError) as exc_info:
        _push(client, managed)

    assert exc_info.value.context["reason"] == "invalid_asset_receipt_proof"
    assert client.page_get_calls == 0
    assert client.list_calls == 0
    assert client.put_calls == 0
    assert client.upload_calls == 0
    assert client.remote_mutations == 0
