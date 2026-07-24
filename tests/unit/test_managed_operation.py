from __future__ import annotations

from dataclasses import dataclass

import pytest

from atlassian_skills.confluence.managed_operation import (
    ManagedAssetOperation,
    ManagedOperation,
    ManagedOperationError,
    RecoveryStatus,
    attachment_inventory_sha256,
    parse_managed_asset_operations,
    parse_managed_operation,
    replace_managed_asset_operation,
    serialize_managed_asset_operation,
    serialize_managed_operation,
    strip_managed_operation,
)


@dataclass(frozen=True)
class FaultCase:
    row: str
    body: str
    assets: str
    observation: str
    expected: RecoveryStatus
    allowed_next_writes: int


FAULT_MATRIX = (
    FaultCase("A1", "source", "baseline", "none", RecoveryStatus.NOT_STARTED, 2),
    FaultCase("A2", "source", "partial", "upload_response_loss", RecoveryStatus.ASSETS_APPLIED_BODY_PENDING, 1),
    FaultCase("A3", "source", "ambiguous", "upload_response_loss", RecoveryStatus.MANUAL_RECOVERY, 0),
    FaultCase("B1", "expected", "expected", "body_response_loss", RecoveryStatus.RECONCILED, 0),
    FaultCase("B2", "expected", "expected", "mismatched_storage", RecoveryStatus.READBACK_PENDING, 0),
    FaultCase("B3", "source", "expected", "body_not_applied", RecoveryStatus.ASSETS_APPLIED_BODY_PENDING, 1),
    FaultCase("C1", "source", "baseline", "local_body_changed", RecoveryStatus.MANUAL_RECOVERY, 0),
    FaultCase("C2", "source", "baseline", "local_asset_changed", RecoveryStatus.MANUAL_RECOVERY, 0),
    FaultCase("C3", "unrelated", "any", "external_edit", RecoveryStatus.CONFLICT, 0),
    FaultCase("D1", "expected", "expected", "local_finalize_changed", RecoveryStatus.MANUAL_RECOVERY, 0),
    FaultCase("D2", "expected_append", "expected", "suffix_normalized", RecoveryStatus.RECONCILED, 0),
    FaultCase("D3", "expected", "incomplete", "receipt_incomplete", RecoveryStatus.READBACK_PENDING, 0),
)


def _operation() -> ManagedOperation:
    return ManagedOperation(
        operation_id="op_0123456789abcdef0123456789abcdef",
        stage="planned",
        proof="full_migration",
        authority="sha256:" + "0" * 64,
        source_version=11,
        source_storage="sha256:" + "1" * 64,
        source_bytes=17,
        expected_version=12,
        edited_md="sha256:" + "2" * 64,
        candidate="sha256:" + "3" * 64,
        report="sha256:" + "4" * 64,
        assets="sha256:" + "5" * 64,
    )


def test_fault_matrix_covers_every_required_recovery_row() -> None:
    assert [case.row for case in FAULT_MATRIX] == [
        "A1",
        "A2",
        "A3",
        "B1",
        "B2",
        "B3",
        "C1",
        "C2",
        "C3",
        "D1",
        "D2",
        "D3",
    ]
    assert all(case.allowed_next_writes >= 0 for case in FAULT_MATRIX)


def test_operation_marker_round_trips_without_sensitive_payloads() -> None:
    operation = _operation()
    marker = serialize_managed_operation(operation)
    markdown = "<!-- atls:managed v=2 -->\n" + marker + "\nBody\n"

    assert parse_managed_operation(markdown) == operation
    assert strip_managed_operation(markdown) == "<!-- atls:managed v=2 -->\nBody\n"
    assert "<p>" not in marker
    assert "Body" not in marker
    assert "token" not in marker.casefold()
    assert "/" not in marker


def test_operation_marker_rejects_duplicate_or_tampered_authority() -> None:
    marker = serialize_managed_operation(_operation())
    with pytest.raises(ManagedOperationError, match="duplicate"):
        parse_managed_operation(marker + "\n" + marker + "\n")
    with pytest.raises(ManagedOperationError, match="invalid_operation_hash"):
        parse_managed_operation(marker.replace("sha256:" + "3" * 64, "sha256:bad"))


def test_asset_receipt_round_trips_exact_identity_without_sensitive_payloads() -> None:
    receipt = ManagedAssetOperation(
        operation_id=_operation().operation_id,
        action="update",
        request_ordinal=0,
        materialization="local",
        src="page.assets/diagram 1.png",
        local_sha256="sha256:" + "6" * 64,
        remote_name="diagram 1.png",
        baseline_id="att-17",
        baseline_version=4,
        baseline_sha256="sha256:" + "7" * 64,
        pre_upload_ids=None,
    )
    marker = serialize_managed_asset_operation(receipt)
    markdown = f"![x](page.assets/diagram%201.png){marker}\n"

    assert parse_managed_asset_operations(markdown) == (receipt,)
    assert "page.assets/diagram%201.png" in marker
    assert "baseline_id=att-17" in marker
    assert "pre_upload_ids=absent" in marker
    assert "password" not in marker.casefold()
    assert "<ac:" not in marker
    assert "/home/" not in marker


def test_asset_receipt_applied_transition_is_durable_and_exact() -> None:
    receipt = ManagedAssetOperation(
        operation_id=_operation().operation_id,
        action="create",
        request_ordinal=0,
        materialization="local",
        src="page.assets/new.png",
        local_sha256="sha256:" + "8" * 64,
        remote_name="new.png",
        baseline_id=None,
        baseline_version=None,
        baseline_sha256=None,
        pre_upload_ids=attachment_inventory_sha256(("att-1",)),
    )
    unknown = receipt.transition("upload_unknown")
    applied = unknown.transition(
        "applied",
        result_id="att-new",
        result_version=1,
        result_sha256=receipt.local_sha256,
    )
    markdown = serialize_managed_asset_operation(unknown) + "\n"

    replaced = replace_managed_asset_operation(markdown, applied)

    assert parse_managed_asset_operations(replaced) == (applied,)
    assert "status=applied" in replaced
    assert "result_id=att-new" in replaced
    assert f"pre_upload_ids={receipt.pre_upload_ids}" in replaced


def test_asset_receipt_rejects_duplicate_ordinal_partial_baseline_and_absolute_path() -> None:
    marker = serialize_managed_asset_operation(
        ManagedAssetOperation(
            operation_id=_operation().operation_id,
            action="update",
            request_ordinal=0,
            materialization="local",
            src="page.assets/diagram.png",
            local_sha256="sha256:" + "8" * 64,
            remote_name="diagram.png",
            baseline_id="att-1",
            baseline_version=1,
            baseline_sha256="sha256:" + "9" * 64,
            pre_upload_ids=None,
        )
    )
    with pytest.raises(ManagedOperationError, match="duplicate_asset_request_ordinal"):
        parse_managed_asset_operations(marker + marker)
    with pytest.raises(ManagedOperationError, match="incomplete_asset_baseline"):
        parse_managed_asset_operations(marker.replace("baseline_version=1", "baseline_version=absent"))
    with pytest.raises(ManagedOperationError, match="invalid_asset_path"):
        parse_managed_asset_operations(marker.replace("src=page.assets/diagram.png", "src=%2Ftmp%2Fsecret"))
