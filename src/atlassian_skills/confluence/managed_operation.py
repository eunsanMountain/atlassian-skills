"""Portable managed-Markdown operation journal.

The journal is deliberately compact: it carries only versions, proof modes,
and domain-separated hashes.  Remote storage, Markdown bodies, credentials,
absolute paths, and attachment bytes never become durable recovery state.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any
from urllib.parse import quote, unquote_to_bytes, urlsplit

from atlassian_skills.core.managed_manifest import (
    ManagedAssetRecord,
    ManagedManifest,
    serialize_asset_record,
    serialize_managed_manifest,
)

_MARKER_PREFIX = "<!-- atls:operation "
_MARKER_SUFFIX = " -->"
_FENCE_RE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)\Z")
_OPERATION_ID_RE = re.compile(r"op_[0-9a-f]{32}\Z")
_HASH_RE = re.compile(r"(?:sha256|append_sha256):[0-9a-f]{64}\Z")
_STAGES = frozenset(
    {
        "planned",
        "assets_applied_body_pending",
        "body_applied_readback_pending",
        "readback_pending",
        "manual_recovery",
        "conflict",
    }
)
_PROOFS = frozenset({"full_migration", "exact_remote_prefix_append"})
_REQUIRED_FIELDS = (
    "v",
    "id",
    "stage",
    "proof",
    "authority",
    "source_version",
    "source_storage",
    "source_bytes",
    "expected_version",
    "edited_md",
    "candidate",
    "assets",
    "proof_bundle",
)
_OPTIONAL_FIELDS = (
    "report",
    "append",
    "fragment_md",
    "fragment_md_bytes",
    "fragment_storage",
    "remote_prefix",
)
_ASSET_OPERATION_PREFIX = "<!-- cfxmark:asset op="
_ASSET_ACTIONS = frozenset({"unchanged", "create", "update"})
_ASSET_STATUSES = frozenset({"planned", "upload_unknown", "applied", "conflict"})
_ASSET_ID_RE = re.compile(r"[A-Za-z0-9._:-]+\Z")
_ASSET_IMAGE_RE = re.compile(
    r"!\[(?:\\.|[^\]])*\]\((?P<target><(?:\\.|[^>])*>|(?:\\.|[^)])+)\)"
    r"(?P<img_metadata><!-- cfxmark:img(?: [^<>]*)? -->)?"
)
_ANY_ASSET_COMMENT_RE = re.compile(r"<!-- cfxmark:asset\b.*? -->")
_INVALID_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class ManagedOperationError(ValueError):
    """A durable operation marker is malformed, duplicated, or inconsistent."""


class RecoveryStatus(str, Enum):
    NOT_STARTED = "not_started"
    ASSETS_APPLIED_BODY_PENDING = "assets_applied_body_pending"
    BODY_APPLIED_READBACK_PENDING = "body_applied_readback_pending"
    RECONCILED = "reconciled"
    READBACK_PENDING = "readback_pending"
    MANUAL_RECOVERY = "manual_recovery"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class ManagedOperation:
    operation_id: str
    stage: str
    proof: str
    authority: str
    source_version: int
    source_storage: str
    source_bytes: int
    expected_version: int
    edited_md: str
    candidate: str
    assets: str
    proof_bundle: str | None = None
    report: str | None = None
    append: str | None = None
    fragment_md: str | None = None
    fragment_md_bytes: int | None = None
    fragment_storage: str | None = None
    remote_prefix: str | None = None
    version: int = 2

    def __post_init__(self) -> None:
        if self.version != 2:
            raise ManagedOperationError("unsupported_operation_version")
        if _OPERATION_ID_RE.fullmatch(self.operation_id) is None:
            raise ManagedOperationError("invalid_operation_id")
        if self.stage not in _STAGES:
            raise ManagedOperationError("invalid_operation_stage")
        if self.proof not in _PROOFS:
            raise ManagedOperationError("invalid_operation_proof")
        if self.source_version < 1 or self.expected_version != self.source_version + 1:
            raise ManagedOperationError("invalid_operation_version_transition")
        for value in (self.authority, self.source_storage, self.edited_md, self.candidate, self.assets):
            if _HASH_RE.fullmatch(value) is None or not value.startswith("sha256:"):
                raise ManagedOperationError("invalid_operation_hash")
        if self.source_bytes < 0:
            raise ManagedOperationError("invalid_operation_source_bytes")
        if self.proof == "full_migration":
            if self.report is None or _HASH_RE.fullmatch(self.report) is None or not self.report.startswith("sha256:"):
                raise ManagedOperationError("invalid_operation_hash")
            if any(
                value is not None
                for value in (
                    self.append,
                    self.fragment_md,
                    self.fragment_md_bytes,
                    self.fragment_storage,
                    self.remote_prefix,
                )
            ):
                raise ManagedOperationError("invalid_full_migration_operation")
        else:
            if self.report is not None:
                raise ManagedOperationError("invalid_append_operation")
            if self.append is None or not self.append.startswith("append_sha256:"):
                raise ManagedOperationError("invalid_operation_hash")
            for optional_value in (self.append, self.fragment_md, self.fragment_storage, self.remote_prefix):
                if optional_value is None or _HASH_RE.fullmatch(optional_value) is None:
                    raise ManagedOperationError("invalid_operation_hash")
            if self.fragment_md_bytes is None or self.fragment_md_bytes < 1:
                raise ManagedOperationError("invalid_operation_fragment_bytes")
            if self.remote_prefix != self.source_storage:
                raise ManagedOperationError("invalid_operation_remote_prefix")
        expected_bundle = operation_proof_bundle_sha256(self)
        if self.proof_bundle is None:
            object.__setattr__(self, "proof_bundle", expected_bundle)
        elif self.proof_bundle != expected_bundle:
            raise ManagedOperationError("invalid_operation_bundle")

    @property
    def status(self) -> RecoveryStatus:
        if self.stage == "planned":
            return RecoveryStatus.NOT_STARTED
        return RecoveryStatus(self.stage)

    def transition(self, stage: str) -> ManagedOperation:
        desired = replace(self, stage=stage)
        allowed: dict[str, frozenset[str]] = {
            "planned": frozenset(
                {
                    "assets_applied_body_pending",
                    "body_applied_readback_pending",
                    "readback_pending",
                    "manual_recovery",
                    "conflict",
                }
            ),
            "assets_applied_body_pending": frozenset(
                {"body_applied_readback_pending", "readback_pending", "manual_recovery", "conflict"}
            ),
            "body_applied_readback_pending": frozenset({"readback_pending", "manual_recovery", "conflict"}),
            "readback_pending": frozenset({"manual_recovery", "conflict"}),
            "manual_recovery": frozenset(),
            "conflict": frozenset(),
        }
        if desired.stage not in allowed[self.stage]:
            raise ManagedOperationError("invalid_operation_transition")
        return desired


@dataclass(frozen=True)
class ManagedAssetOperation:
    operation_id: str
    action: str
    request_ordinal: int
    materialization: str
    src: str
    local_sha256: str
    remote_name: str
    baseline_id: str | None
    baseline_version: int | None
    baseline_sha256: str | None
    pre_upload_ids: str | None
    operation_proof: str | None = None
    status: str = "planned"
    result_id: str | None = None
    result_version: int | None = None
    result_sha256: str | None = None
    receipt_proof: str | None = None

    def __post_init__(self) -> None:
        if _OPERATION_ID_RE.fullmatch(self.operation_id) is None:
            raise ManagedOperationError("invalid_asset_operation_id")
        if self.action not in _ASSET_ACTIONS or self.status not in _ASSET_STATUSES:
            raise ManagedOperationError("invalid_asset_operation_state")
        if self.request_ordinal < 0:
            raise ManagedOperationError("invalid_asset_request_ordinal")
        if self.materialization not in {"local", "remote-only"}:
            raise ManagedOperationError("invalid_asset_materialization")
        if not self.src or "\\" in self.src or self.src.startswith("/"):
            raise ManagedOperationError("invalid_asset_path")
        if any(part in {"", ".", ".."} for part in self.src.split("/")):
            raise ManagedOperationError("invalid_asset_path")
        if not self.remote_name or "/" in self.remote_name or "\\" in self.remote_name:
            raise ManagedOperationError("invalid_asset_remote_name")
        if _HASH_RE.fullmatch(self.local_sha256) is None or not self.local_sha256.startswith("sha256:"):
            raise ManagedOperationError("invalid_asset_hash")
        baseline = (self.baseline_id, self.baseline_version, self.baseline_sha256)
        if any(value is None for value in baseline) and not all(value is None for value in baseline):
            raise ManagedOperationError("incomplete_asset_baseline")
        if self.action in {"unchanged", "update"} and self.baseline_id is None:
            raise ManagedOperationError("asset_baseline_required")
        if self.action == "create" and self.baseline_id is not None:
            raise ManagedOperationError("create_asset_has_baseline")
        if self.action == "create":
            if (
                self.pre_upload_ids is None
                or _HASH_RE.fullmatch(self.pre_upload_ids) is None
                or not self.pre_upload_ids.startswith("sha256:")
            ):
                raise ManagedOperationError("create_asset_inventory_required")
        elif self.pre_upload_ids is not None:
            raise ManagedOperationError("unexpected_asset_inventory")
        if self.baseline_id is not None:
            if _ASSET_ID_RE.fullmatch(self.baseline_id) is None or self.baseline_version is None:
                raise ManagedOperationError("invalid_asset_baseline")
            if self.baseline_version < 1 or self.baseline_sha256 is None:
                raise ManagedOperationError("invalid_asset_baseline")
            if _HASH_RE.fullmatch(self.baseline_sha256) is None or not self.baseline_sha256.startswith("sha256:"):
                raise ManagedOperationError("invalid_asset_hash")
        result = (self.result_id, self.result_version, self.result_sha256)
        if self.status == "applied":
            if any(value is None for value in result):
                raise ManagedOperationError("incomplete_asset_result")
            if self.result_id is None or _ASSET_ID_RE.fullmatch(self.result_id) is None:
                raise ManagedOperationError("invalid_asset_result")
            if self.result_version is None or self.result_version < 1:
                raise ManagedOperationError("invalid_asset_result")
            if self.result_sha256 != self.local_sha256:
                raise ManagedOperationError("asset_result_hash_mismatch")
        elif any(value is not None for value in result):
            raise ManagedOperationError("unexpected_asset_result")
        operation_proof = self.operation_proof
        if operation_proof is None:
            operation_proof = _sha256_text(f"atls-unbound-asset-operation-v1:{self.operation_id}")
            object.__setattr__(self, "operation_proof", operation_proof)
        if _HASH_RE.fullmatch(operation_proof) is None or not operation_proof.startswith("sha256:"):
            raise ManagedOperationError("invalid_asset_operation_proof")
        expected_receipt_proof = asset_receipt_proof_sha256(self)
        if self.receipt_proof is None:
            object.__setattr__(self, "receipt_proof", expected_receipt_proof)
        elif self.receipt_proof != expected_receipt_proof:
            raise ManagedOperationError("invalid_asset_receipt_proof")

    def transition(
        self,
        status: str,
        *,
        result_id: str | None = None,
        result_version: int | None = None,
        result_sha256: str | None = None,
    ) -> ManagedAssetOperation:
        allowed = {
            "planned": frozenset({"upload_unknown", "applied", "conflict"}),
            "upload_unknown": frozenset({"planned", "applied", "conflict"}),
            "applied": frozenset(),
            "conflict": frozenset(),
        }
        if status not in allowed[self.status]:
            raise ManagedOperationError("invalid_asset_operation_transition")
        return replace(
            self,
            status=status,
            result_id=result_id,
            result_version=result_version,
            result_sha256=result_sha256,
            receipt_proof=None,
        )


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def attachment_inventory_sha256(attachment_ids: Iterable[str]) -> str:
    identifiers = sorted(attachment_ids)
    if len(identifiers) != len(set(identifiers)) or any(_ASSET_ID_RE.fullmatch(item) is None for item in identifiers):
        raise ManagedOperationError("invalid_asset_inventory")
    return _sha256_text(json.dumps(identifiers, ensure_ascii=True, separators=(",", ":")))


def managed_operation_authority(manifest: ManagedManifest) -> str:
    return _sha256_text(serialize_managed_manifest(manifest))


def operation_proof_bundle_sha256(operation: ManagedOperation) -> str:
    payload = {
        "schema": "atls-operation-proof-bundle-v2",
        "version": operation.version,
        "operation_id": operation.operation_id,
        "proof": operation.proof,
        "authority": operation.authority,
        "source_version": operation.source_version,
        "source_storage": operation.source_storage,
        "source_bytes": operation.source_bytes,
        "expected_version": operation.expected_version,
        "edited_md": operation.edited_md,
        "candidate": operation.candidate,
        "assets": operation.assets,
        "report": operation.report,
        "append": operation.append,
        "fragment_md": operation.fragment_md,
        "fragment_md_bytes": operation.fragment_md_bytes,
        "fragment_storage": operation.fragment_storage,
        "remote_prefix": operation.remote_prefix,
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return _sha256_text(encoded)


def asset_receipt_proof_sha256(asset: ManagedAssetOperation) -> str:
    payload = {
        "schema": "atls-asset-receipt-proof-v1",
        "operation_proof": asset.operation_proof,
        "operation_id": asset.operation_id,
        "action": asset.action,
        "request_ordinal": asset.request_ordinal,
        "materialization": asset.materialization,
        "src": asset.src,
        "local_sha256": asset.local_sha256,
        "remote_name": asset.remote_name,
        "baseline_id": asset.baseline_id,
        "baseline_version": asset.baseline_version,
        "baseline_sha256": asset.baseline_sha256,
        "pre_upload_ids": asset.pre_upload_ids,
        "status": asset.status,
        "result_id": asset.result_id,
        "result_version": asset.result_version,
        "result_sha256": asset.result_sha256,
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return _sha256_text(encoded)


def _encode_asset_value(value: str, *, path: bool = False) -> str:
    return quote(value, safe="/-._~" if path else "-._~")


def _decode_asset_value(value: str, *, field: str) -> str:
    if not value or _INVALID_PERCENT_RE.search(value):
        raise ManagedOperationError(f"invalid_asset_{field}")
    try:
        return unquote_to_bytes(value).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ManagedOperationError(f"invalid_asset_{field}") from error


def serialize_managed_asset_operation(asset: ManagedAssetOperation) -> str:
    assert asset.operation_proof is not None
    assert asset.receipt_proof is not None
    values = [
        ("op", asset.operation_id),
        ("action", asset.action),
        ("request_ordinal", str(asset.request_ordinal)),
        ("materialization", asset.materialization),
        ("src", _encode_asset_value(asset.src, path=True)),
        ("local_sha256", asset.local_sha256),
        ("remote_name", _encode_asset_value(asset.remote_name)),
        ("baseline_id", "absent" if asset.baseline_id is None else _encode_asset_value(asset.baseline_id)),
        ("baseline_version", "absent" if asset.baseline_version is None else str(asset.baseline_version)),
        ("baseline_sha256", "absent" if asset.baseline_sha256 is None else asset.baseline_sha256),
        ("pre_upload_ids", "absent" if asset.pre_upload_ids is None else asset.pre_upload_ids),
        ("operation_proof", asset.operation_proof),
        ("status", asset.status),
    ]
    if asset.status == "applied":
        assert asset.result_id is not None
        assert asset.result_version is not None
        assert asset.result_sha256 is not None
        values.extend(
            (
                ("result_id", _encode_asset_value(asset.result_id)),
                ("result_version", str(asset.result_version)),
                ("result_sha256", asset.result_sha256),
            )
        )
    values.append(("receipt_proof", asset.receipt_proof))
    return "<!-- cfxmark:asset " + " ".join(f"{name}={value}" for name, value in values) + " -->"


def _parse_managed_asset_operation(marker: str) -> ManagedAssetOperation:
    prefix = "<!-- cfxmark:asset "
    if not marker.startswith(prefix) or not marker.endswith(_MARKER_SUFFIX):
        raise ManagedOperationError("malformed_asset_operation_marker")
    tokens = marker[len(prefix) : -len(_MARKER_SUFFIX)].split(" ")
    values: dict[str, str] = {}
    names: list[str] = []
    for token in tokens:
        name, separator, value = token.partition("=")
        if not separator or not name or not value or name in values:
            raise ManagedOperationError("malformed_asset_operation_marker")
        names.append(name)
        values[name] = value
    required = [
        "op",
        "action",
        "request_ordinal",
        "materialization",
        "src",
        "local_sha256",
        "remote_name",
        "baseline_id",
        "baseline_version",
        "baseline_sha256",
        "pre_upload_ids",
        "operation_proof",
        "status",
    ]
    if values.get("status") == "applied":
        required.extend(("result_id", "result_version", "result_sha256"))
    required.append("receipt_proof")
    if names != required:
        raise ManagedOperationError("malformed_asset_operation_marker")

    def absent(name: str) -> str | None:
        return None if values[name] == "absent" else values[name]

    baseline_id = absent("baseline_id")
    baseline_version = absent("baseline_version")
    baseline_sha256 = absent("baseline_sha256")
    try:
        return ManagedAssetOperation(
            operation_id=values["op"],
            action=values["action"],
            request_ordinal=int(values["request_ordinal"]),
            materialization=values["materialization"],
            src=_decode_asset_value(values["src"], field="path"),
            local_sha256=values["local_sha256"],
            remote_name=_decode_asset_value(values["remote_name"], field="remote_name"),
            baseline_id=(None if baseline_id is None else _decode_asset_value(baseline_id, field="baseline_id")),
            baseline_version=None if baseline_version is None else int(baseline_version),
            baseline_sha256=baseline_sha256,
            pre_upload_ids=absent("pre_upload_ids"),
            operation_proof=values["operation_proof"],
            status=values["status"],
            result_id=(_decode_asset_value(values["result_id"], field="result_id") if "result_id" in values else None),
            result_version=int(values["result_version"]) if "result_version" in values else None,
            result_sha256=values.get("result_sha256"),
            receipt_proof=values["receipt_proof"],
        )
    except ValueError as error:
        if isinstance(error, ManagedOperationError):
            raise
        raise ManagedOperationError("malformed_asset_operation_marker") from error


def parse_managed_asset_operations(markdown: str) -> tuple[ManagedAssetOperation, ...]:
    parsed: list[ManagedAssetOperation] = []
    fence: tuple[str, int] | None = None
    for raw_line in markdown.removeprefix("\ufeff").splitlines():
        fence_match = _FENCE_RE.fullmatch(raw_line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1] and not fence_match.group(2).strip():
                fence = None
            continue
        if fence is not None or _ASSET_OPERATION_PREFIX not in raw_line:
            continue
        matches = [
            match.group(0)
            for match in _ANY_ASSET_COMMENT_RE.finditer(raw_line)
            if match.group(0).startswith(_ASSET_OPERATION_PREFIX)
        ]
        if len(matches) != raw_line.count(_ASSET_OPERATION_PREFIX):
            raise ManagedOperationError("malformed_asset_operation_marker")
        parsed.extend(_parse_managed_asset_operation(marker) for marker in matches)
    operation_ids = {asset.operation_id for asset in parsed}
    ordinals = [asset.request_ordinal for asset in parsed]
    if len(operation_ids) > 1:
        raise ManagedOperationError("asset_operation_id_mismatch")
    if len(ordinals) != len(set(ordinals)):
        raise ManagedOperationError("duplicate_asset_request_ordinal")
    if ordinals and sorted(ordinals) != list(range(len(ordinals))):
        raise ManagedOperationError("noncontiguous_asset_request_ordinal")
    srcs = [asset.src for asset in parsed]
    if len(srcs) != len(set(srcs)):
        raise ManagedOperationError("duplicate_asset_source")
    return tuple(sorted(parsed, key=lambda asset: asset.request_ordinal))


def _visible_asset_target(target: str) -> str | None:
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or not parsed.path or parsed.query or parsed.fragment:
        return None
    try:
        return unquote_to_bytes(parsed.path).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ManagedOperationError("invalid_asset_path") from error


def _asset_image_bindings(markdown: str) -> dict[str, list[tuple[int, int]]]:
    bindings: dict[str, list[tuple[int, int]]] = {}
    fence: tuple[str, int] | None = None
    offset = 0
    for raw_line in markdown.splitlines(keepends=True):
        line = raw_line.removesuffix("\n")
        fence_match = _FENCE_RE.fullmatch(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1] and not fence_match.group(2).strip():
                fence = None
            offset += len(raw_line)
            continue
        if fence is None:
            for match in _ASSET_IMAGE_RE.finditer(line):
                src = _visible_asset_target(match.group("target"))
                if src is None:
                    continue
                marker = _ANY_ASSET_COMMENT_RE.match(line, match.end())
                start = offset + match.end()
                end = offset + (marker.end() if marker is not None else match.end())
                bindings.setdefault(src, []).append((start, end))
        offset += len(raw_line)
    return bindings


def stage_managed_asset_operations(
    markdown: str,
    operation: ManagedOperation,
    asset_plan: tuple[Any, ...],
) -> str:
    if parse_managed_asset_operations(markdown):
        raise ManagedOperationError("duplicate_asset_operation_marker")
    bindings = _asset_image_bindings(markdown)
    replacements: list[tuple[int, int, str]] = []
    request_ordinal = 0
    assert operation.proof_bundle is not None
    for item in asset_plan:
        if item.action == "unreferenced":
            continue
        matches = bindings.get(item.src, [])
        if len(matches) != 1:
            raise ManagedOperationError("asset_image_binding_missing_or_ambiguous")
        baseline_id = item.remote_id
        baseline_version = item.remote_version
        baseline_sha256 = item.baseline_sha256
        status = "planned"
        result_id = None
        result_version = None
        result_sha256 = None
        if item.action == "unchanged":
            if baseline_id is None or baseline_version is None or baseline_sha256 is None:
                raise ManagedOperationError("asset_baseline_required")
            status = "applied"
            result_id = baseline_id
            result_version = baseline_version
            result_sha256 = item.current_sha256
        asset = ManagedAssetOperation(
            operation_id=operation.operation_id,
            action=item.action,
            request_ordinal=request_ordinal,
            materialization=item.materialization,
            src=item.src,
            local_sha256=item.current_sha256,
            remote_name=item.remote_name,
            baseline_id=baseline_id,
            baseline_version=baseline_version,
            baseline_sha256=baseline_sha256,
            pre_upload_ids=item.pre_upload_ids,
            operation_proof=operation.proof_bundle,
            status=status,
            result_id=result_id,
            result_version=result_version,
            result_sha256=result_sha256,
        )
        start, end = matches[0]
        replacements.append((start, end, serialize_managed_asset_operation(asset)))
        request_ordinal += 1
    result = markdown
    for start, end, replacement in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]
    parsed = parse_managed_asset_operations(result)
    if any(asset.operation_id != operation.operation_id for asset in parsed) or len(parsed) != request_ordinal:
        raise ManagedOperationError("asset_operation_binding_failed")
    return result


def replace_managed_asset_operation(markdown: str, asset: ManagedAssetOperation) -> str:
    current = parse_managed_asset_operations(markdown)
    matches = [item for item in current if item.request_ordinal == asset.request_ordinal]
    if len(matches) != 1 or matches[0].operation_id != asset.operation_id:
        raise ManagedOperationError("asset_operation_marker_missing_or_duplicate")
    marker = serialize_managed_asset_operation(matches[0])
    if markdown.count(marker) != 1:
        raise ManagedOperationError("asset_operation_marker_missing_or_duplicate")
    return markdown.replace(marker, serialize_managed_asset_operation(asset), 1)


def finalize_managed_asset_operations(markdown: str, operation_id: str) -> str:
    assets = parse_managed_asset_operations(markdown)
    if any(asset.operation_id != operation_id for asset in assets):
        raise ManagedOperationError("asset_operation_id_mismatch")
    result = markdown
    for asset in assets:
        if asset.status != "applied" or asset.result_id is None or asset.result_version is None:
            raise ManagedOperationError("asset_operation_not_reconciled")
        record = ManagedAssetRecord(
            materialization=asset.materialization,
            src=asset.src,
            remote_id=asset.result_id,
            remote_version=asset.result_version,
            remote_name=asset.remote_name,
            sha256=asset.local_sha256,
        )
        marker = serialize_managed_asset_operation(asset)
        if result.count(marker) != 1:
            raise ManagedOperationError("asset_operation_marker_missing_or_duplicate")
        result = result.replace(marker, serialize_asset_record(record), 1)
    if parse_managed_asset_operations(result):
        raise ManagedOperationError("asset_operation_cleanup_failed")
    return result


def asset_operation_token(asset: ManagedAssetOperation) -> str:
    return f"atls-op:{asset.operation_id}:{asset.request_ordinal}:{asset.local_sha256[7:19]}"


def operation_for_preflight(preflight: Any, *, operation_id: str | None = None) -> ManagedOperation:
    common: dict[str, Any] = {
        "operation_id": operation_id or f"op_{secrets.token_hex(16)}",
        "stage": "planned",
        "proof": preflight.proof_mode,
        "authority": managed_operation_authority(preflight.document.manifest),
        "source_version": preflight.remote_version,
        "source_storage": preflight.remote_storage_sha256,
        "source_bytes": len(preflight.source_storage.encode("utf-8")),
        "expected_version": preflight.remote_version + 1,
        "edited_md": _sha256_text(preflight.edited_markdown),
        "candidate": preflight.candidate_storage_sha256,
        "assets": preflight.asset_plan_sha256,
    }
    if preflight.proof_mode == "full_migration":
        common["report"] = preflight.migration_report_sha256
    elif preflight.proof_mode == "exact_remote_prefix_append":
        suffix = preflight.candidate_storage[len(preflight.source_storage) :]
        fragment = preflight.edited_markdown[len(preflight.base_markdown + "\n") :]
        common.update(
            {
                "append": preflight.append_sha256,
                "fragment_md": preflight.append_fragment_sha256,
                "fragment_md_bytes": len(fragment.encode("utf-8")),
                "fragment_storage": _sha256_text(suffix),
                "remote_prefix": preflight.remote_storage_sha256,
            }
        )
    else:
        raise ManagedOperationError("operation_requires_body_change")
    return ManagedOperation(**common)


def serialize_managed_operation(operation: ManagedOperation) -> str:
    values: list[tuple[str, str]] = [
        ("v", str(operation.version)),
        ("id", operation.operation_id),
        ("stage", operation.stage),
        ("proof", operation.proof),
        ("authority", operation.authority),
        ("source_version", str(operation.source_version)),
        ("source_storage", operation.source_storage),
        ("source_bytes", str(operation.source_bytes)),
        ("expected_version", str(operation.expected_version)),
        ("edited_md", operation.edited_md),
        ("candidate", operation.candidate),
    ]
    for name in _OPTIONAL_FIELDS:
        value = getattr(operation, name)
        if value is not None:
            values.append((name, str(value)))
    values.append(("assets", operation.assets))
    assert operation.proof_bundle is not None
    values.append(("proof_bundle", operation.proof_bundle))
    payload = " ".join(f"{name}={value}" for name, value in values)
    return f"{_MARKER_PREFIX}{payload}{_MARKER_SUFFIX}"


def _operation_lines(markdown: str) -> tuple[tuple[int, str], ...]:
    matches: list[tuple[int, str]] = []
    fence: tuple[str, int] | None = None
    for ordinal, raw_line in enumerate(markdown.removeprefix("\ufeff").splitlines()):
        fence_match = _FENCE_RE.fullmatch(raw_line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1] and not fence_match.group(2).strip():
                fence = None
            continue
        if fence is None and _MARKER_PREFIX in raw_line:
            matches.append((ordinal, raw_line))
    return tuple(matches)


def parse_managed_operation(markdown: str) -> ManagedOperation | None:
    matches = _operation_lines(markdown)
    if len(matches) > 1:
        raise ManagedOperationError("duplicate_operation_marker")
    if not matches:
        return None
    _ordinal, line = matches[0]
    if not line.startswith(_MARKER_PREFIX) or not line.endswith(_MARKER_SUFFIX):
        raise ManagedOperationError("malformed_operation_marker")
    tokens = line[len(_MARKER_PREFIX) : -len(_MARKER_SUFFIX)].split(" ")
    values: dict[str, str] = {}
    for token in tokens:
        name, separator, value = token.partition("=")
        if not separator or not name or not value or name in values:
            raise ManagedOperationError("malformed_operation_marker")
        values[name] = value
    allowed = set(_REQUIRED_FIELDS) | set(_OPTIONAL_FIELDS)
    if set(values) - allowed or any(name not in values for name in _REQUIRED_FIELDS):
        raise ManagedOperationError("malformed_operation_marker")
    try:
        return ManagedOperation(
            version=int(values["v"]),
            operation_id=values["id"],
            stage=values["stage"],
            proof=values["proof"],
            authority=values["authority"],
            source_version=int(values["source_version"]),
            source_storage=values["source_storage"],
            source_bytes=int(values["source_bytes"]),
            expected_version=int(values["expected_version"]),
            edited_md=values["edited_md"],
            candidate=values["candidate"],
            assets=values["assets"],
            proof_bundle=values["proof_bundle"],
            report=values.get("report"),
            append=values.get("append"),
            fragment_md=values.get("fragment_md"),
            fragment_md_bytes=(int(values["fragment_md_bytes"]) if "fragment_md_bytes" in values else None),
            fragment_storage=values.get("fragment_storage"),
            remote_prefix=values.get("remote_prefix"),
        )
    except ValueError as error:
        if isinstance(error, ManagedOperationError):
            raise
        raise ManagedOperationError("malformed_operation_marker") from error


def insert_managed_operation(markdown: str, operation: ManagedOperation) -> str:
    if parse_managed_operation(markdown) is not None:
        raise ManagedOperationError("duplicate_operation_marker")
    first, separator, remainder = markdown.partition("\n")
    if not separator:
        raise ManagedOperationError("managed_manifest_line_missing")
    return first + "\n" + serialize_managed_operation(operation) + "\n" + remainder


def replace_managed_operation(markdown: str, operation: ManagedOperation) -> str:
    matches = _operation_lines(markdown)
    if len(matches) != 1:
        raise ManagedOperationError("operation_marker_missing_or_duplicate")
    lines = markdown.splitlines(keepends=True)
    ordinal, _line = matches[0]
    newline = "\n" if lines[ordinal].endswith("\n") else ""
    lines[ordinal] = serialize_managed_operation(operation) + newline
    return "".join(lines)


def strip_managed_operation(markdown: str) -> str:
    matches = _operation_lines(markdown)
    if len(matches) > 1:
        raise ManagedOperationError("duplicate_operation_marker")
    if not matches:
        return markdown
    lines = markdown.splitlines(keepends=True)
    ordinal, line = matches[0]
    if not line.startswith(_MARKER_PREFIX) or not line.endswith(_MARKER_SUFFIX):
        raise ManagedOperationError("malformed_operation_marker")
    del lines[ordinal]
    return "".join(lines)


__all__ = [
    "ManagedAssetOperation",
    "ManagedOperation",
    "ManagedOperationError",
    "RecoveryStatus",
    "attachment_inventory_sha256",
    "asset_operation_token",
    "finalize_managed_asset_operations",
    "insert_managed_operation",
    "managed_operation_authority",
    "operation_for_preflight",
    "operation_proof_bundle_sha256",
    "parse_managed_asset_operations",
    "parse_managed_operation",
    "replace_managed_asset_operation",
    "replace_managed_operation",
    "serialize_managed_operation",
    "serialize_managed_asset_operation",
    "stage_managed_asset_operations",
    "strip_managed_operation",
]
