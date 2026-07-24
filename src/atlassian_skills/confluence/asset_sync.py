"""Typed attachment synchronization and recovery contracts."""

from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import cfxmark

from atlassian_skills.confluence.managed_operation import ManagedAssetOperation, attachment_inventory_sha256
from atlassian_skills.core.errors import ValidationError
from atlassian_skills.core.file_identity import inspect_file_identity
from atlassian_skills.core.managed_file import resolve_managed_asset_path

_ASSET_MARKER_RE = re.compile(
    r"(?P<prefix>!\[(?:\\.|[^\]])*\]\()"
    r"(?P<target><(?:\\.|[^>])*>|(?:\\.|[^)])+)\)"
    r"(?P<img_metadata><!-- cfxmark:img(?: [^<>]*)? -->)?"
    r'<!-- cfxmark:asset(?: v=(?P<asset_version>[23]))? src="(?P<asset_source>[^"]*)" -->'
)
_IMAGE_REFERENCE_RE = re.compile(r"!\[(?:\\.|[^\]])*\]\((<(?:\\.|[^>])*>|(?:\\.|[^)])+)\)")
_MANAGED_IMAGE_RE = re.compile(
    r"(?P<prefix>!\[(?:\\.|[^\]])*\]\()"
    r"(?P<target><(?:\\.|[^>])*>|(?:\\.|[^)])+)\)"
    r"(?P<img_metadata><!-- cfxmark:img(?: [^<>]*)? -->)?"
)
_IMAGE_METADATA_RE = re.compile(
    r"cfxmark:(?:[wh]=\d+|thumbnail=1|align=(?:center|right))"
    r"(?:,(?:[wh]=\d+|thumbnail=1|align=(?:center|right)))*"
)


def _cfx_asset_marker_source(match: re.Match[str]) -> str:
    source = match.group("asset_source")
    version = match.group("asset_version")
    if version is None:
        return source
    if version == "2":
        padding = "=" * (-len(source) % 4)
        try:
            decoded = base64.b64decode(source + padding, altchars=b"-_", validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as error:
            raise ValueError("cfxmark asset marker encoding is invalid") from error
        canonical = base64.urlsafe_b64encode(decoded.encode("utf-8")).rstrip(b"=").decode("ascii")
    else:
        try:
            decoded = bytes.fromhex(source).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("cfxmark asset marker encoding is invalid") from error
        canonical = decoded.encode("utf-8").hex()
    if source != canonical:
        raise ValueError("cfxmark asset marker encoding is not canonical")
    return decoded


def _serialize_cfx_asset_marker(remote_name: str) -> str:
    if any(character in remote_name for character in '%"\r\n') or "--" in remote_name:
        encoded = remote_name.encode("utf-8").hex()
        return f'<!-- cfxmark:asset v=3 src="{encoded}" -->'
    return f'<!-- cfxmark:asset src="{remote_name}" -->'


class AssetAction(str, Enum):
    UNCHANGED = "unchanged"
    NEW = "new"
    NEW_VERSION = "new-version"
    UNREFERENCED = "unreferenced"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class AssetBaseline:
    attachment_id: str | None
    attachment_version: int | None
    filename: str
    local_path: Path
    content_sha256: str
    media_type: str | None
    reference_state: str


@dataclass(frozen=True)
class RemoteAttachmentSnapshot:
    attachment_id: str
    version: int
    filename: str
    content_sha256: str
    size: int
    media_type: str | None


@dataclass(frozen=True)
class ManagedAssetReference:
    local_reference: str
    remote_filename: str


@dataclass(frozen=True)
class AssetPlanItem:
    reference: str
    local_path: Path
    remote_filename: str
    content_sha256: str | None
    action: AssetAction
    baseline: AssetBaseline | None = None
    remote: RemoteAttachmentSnapshot | None = None
    reason: str | None = None
    local_file_identity: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.pop("local_file_identity", None)
        result["local_path"] = str(self.local_path)
        result["action"] = self.action.value
        if self.baseline is not None:
            result["baseline"]["local_path"] = str(self.baseline.local_path)
        return result


@dataclass(frozen=True)
class AssetPlan:
    items: tuple[AssetPlanItem, ...]
    remote_deletes: tuple[str, ...] = ()

    @property
    def uploads(self) -> tuple[AssetPlanItem, ...]:
        return tuple(item for item in self.items if item.action in {AssetAction.NEW, AssetAction.NEW_VERSION})

    @property
    def conflicts(self) -> tuple[AssetPlanItem, ...]:
        return tuple(item for item in self.items if item.action is AssetAction.CONFLICT)

    @property
    def unchanged(self) -> tuple[AssetPlanItem, ...]:
        return tuple(item for item in self.items if item.action is AssetAction.UNCHANGED)

    @property
    def unreferenced(self) -> tuple[AssetPlanItem, ...]:
        return tuple(item for item in self.items if item.action is AssetAction.UNREFERENCED)

    @property
    def dirty(self) -> bool:
        return bool(self.uploads)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dirty": self.dirty,
            "uploads": [item.to_dict() for item in self.uploads],
            "unchanged": [item.to_dict() for item in self.unchanged],
            "unreferenced": [item.to_dict() for item in self.unreferenced],
            "conflicts": [item.to_dict() for item in self.conflicts],
            "remote_deletes": [],
        }


@dataclass(frozen=True)
class ManagedAssetReconciliation:
    asset: ManagedAssetOperation
    retry_upload: bool = False
    adopted_response_loss: bool = False
    reason: str | None = None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def attachment_version(attachment: Any) -> int:
    version = getattr(attachment, "version", None)
    number = getattr(version, "number", None)
    if isinstance(number, int):
        return number
    if isinstance(version, int):
        return version
    return 1


def remote_id_set_sha256(remote: Iterable[RemoteAttachmentSnapshot]) -> str:
    payload = "\0".join(sorted(item.attachment_id for item in remote)).encode()
    return hashlib.sha256(payload).hexdigest()


def managed_reference(managed_path: Path, local_path: Path) -> str:
    try:
        return local_path.resolve(strict=False).relative_to(managed_path.parent.resolve(strict=False)).as_posix()
    except ValueError as error:
        raise ValueError("managed asset must be within the managed Markdown directory") from error


@dataclass(frozen=True)
class _MarkdownReplacement:
    start: int
    end: int
    value: str


def _rewrite_attachment_markdown(
    markdown: str,
    references: dict[str, str],
) -> tuple[str, tuple[_MarkdownReplacement, ...]]:
    replacements: list[_MarkdownReplacement] = []

    def replace_marker(match: re.Match[str]) -> str:
        reference = references.get(_cfx_asset_marker_source(match))
        if reference is None:
            return match.group(0)
        metadata_match = _IMAGE_METADATA_RE.search(match.group("target"))
        metadata = metadata_match.group(0) if metadata_match else ""
        target = quote(reference, safe="/-._~")
        if metadata:
            target = f"{target}#{metadata}"
        img_metadata = match.group("img_metadata") or ""
        value = f"{match.group('prefix')}{target}){img_metadata}"
        replacements.append(_MarkdownReplacement(match.start(), match.end(), value))
        return value

    return _ASSET_MARKER_RE.sub(replace_marker, markdown), tuple(replacements)


def rewrite_attachment_markdown(markdown: str, references: dict[str, str]) -> str:
    """Rewrite only image URL path components while retaining cfxmark display metadata."""

    rewritten, _replacements = _rewrite_attachment_markdown(markdown, references)
    return rewritten


def rewrite_attachment_artifact(
    artifact: cfxmark.MarkdownArtifact,
    references: dict[str, str],
) -> cfxmark.MarkdownArtifact:
    """Rewrite attachment paths and keep typed Markdown range provenance aligned."""

    rewritten, replacements = _rewrite_attachment_markdown(artifact.markdown, references)
    if not replacements:
        return replace(artifact, markdown=rewritten)

    def rebase_range(markdown_range: cfxmark.MarkdownRange) -> cfxmark.MarkdownRange:
        start = markdown_range.start
        end = markdown_range.end
        before_delta = 0
        inside_delta = 0
        for replacement in replacements:
            delta = len(replacement.value) - (replacement.end - replacement.start)
            if replacement.end <= start:
                before_delta += delta
                continue
            if replacement.start >= end:
                continue
            if start <= replacement.start and replacement.end <= end:
                inside_delta += delta
                continue
            raise ValueError("attachment rewrite crosses a typed Markdown range boundary")
        return cfxmark.MarkdownRange(start=start + before_delta, end=end + before_delta + inside_delta)

    def projection_fingerprint(value: str, *, template: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return f"sha256:{digest}" if template.startswith("sha256:") else digest

    protected_regions: list[cfxmark.ProtectedRegionArtifact] = []
    for region in artifact.protected_regions:
        base_range = region.base_markdown_range
        rebased_range = rebase_range(base_range)
        original_projection = artifact.markdown[base_range.start : base_range.end]
        rebased_projection = rewritten[rebased_range.start : rebased_range.end]
        protected_regions.append(
            replace(
                region,
                base_markdown_range=rebased_range,
                display_label=(
                    rebased_projection if region.display_label == original_projection else region.display_label
                ),
                projection_fingerprint=projection_fingerprint(
                    rebased_projection,
                    template=region.projection_fingerprint,
                ),
            )
        )

    remote_subtrees: list[cfxmark.RemoteSubtreeArtifact] = []
    for subtree in artifact.remote_subtrees:
        rebased_range = rebase_range(subtree.base_markdown_range)
        rebased_projection = rewritten[rebased_range.start : rebased_range.end]
        remote_subtrees.append(
            replace(
                subtree,
                base_markdown_range=rebased_range,
                projection_fingerprint=projection_fingerprint(
                    rebased_projection,
                    template=subtree.projection_fingerprint,
                ),
            )
        )

    return replace(
        artifact,
        markdown=rewritten,
        protected_regions=tuple(protected_regions),
        remote_subtrees=tuple(remote_subtrees),
    )


def extract_managed_asset_references(markdown: str) -> tuple[ManagedAssetReference, ...]:
    references: list[ManagedAssetReference] = []

    def local_reference(visible: str) -> str | None:
        if visible.startswith("<") and visible.endswith(">"):
            visible = visible[1:-1]
        metadata = _IMAGE_METADATA_RE.search(visible)
        if metadata is not None:
            start = metadata.start()
            if start > 0 and visible[start - 1] in {"#", "&"}:
                start -= 1
            visible = (visible[:start] + visible[metadata.end() :]).rstrip("#&")
        url = urlsplit(visible)
        if url.scheme or url.netloc or not url.path:
            return None
        if url.query or url.fragment:
            raise ValueError("managed local attachment query or fragment is not representable")
        return unquote(url.path)

    marker_starts: set[int] = set()
    for match in _ASSET_MARKER_RE.finditer(markdown):
        marker_starts.add(match.start())
        local = local_reference(match.group("target"))
        if local is not None:
            references.append(ManagedAssetReference(local, _cfx_asset_marker_source(match)))
    for match in _IMAGE_REFERENCE_RE.finditer(markdown):
        if match.start() in marker_starts:
            continue
        local = local_reference(match.group(1))
        if local is None:
            continue
        remote_filename = unquote(PurePosixPath(urlsplit(match.group(1).strip("<>")).path).name)
        if not remote_filename:
            continue
        references.append(ManagedAssetReference(local, remote_filename))
    return tuple(references)


def bind_managed_attachment_markdown(markdown: str, filename_map: Mapping[str, str]) -> str:
    """Project portable local image targets back into cfxmark's typed attachment syntax."""

    def replace_image(match: re.Match[str]) -> str:
        visible = match.group("target")
        if visible.startswith("<") and visible.endswith(">"):
            visible = visible[1:-1]
        metadata_match = _IMAGE_METADATA_RE.search(visible)
        metadata = metadata_match.group(0) if metadata_match else ""
        if metadata_match is not None:
            start = metadata_match.start()
            if start > 0 and visible[start - 1] in {"#", "&"}:
                start -= 1
            visible = (visible[:start] + visible[metadata_match.end() :]).rstrip("#&")
        url = urlsplit(visible)
        if url.scheme or url.netloc or not url.path or url.query or url.fragment:
            return match.group(0)
        local_reference = unquote(url.path)
        remote_name = filename_map.get(local_reference)
        if remote_name is None:
            return match.group(0)
        target = quote(remote_name, safe="/-._~")
        if metadata:
            target = f"{target}#{metadata}"
        img_metadata = match.group("img_metadata") or ""
        return f"{match.group('prefix')}{target}){img_metadata}{_serialize_cfx_asset_marker(remote_name)}"

    return _MANAGED_IMAGE_RE.sub(replace_image, markdown)


def attachment_filename_map(
    managed_path: Path,
    baselines: Iterable[AssetBaseline],
) -> tuple[tuple[str, str], ...]:
    mapping: list[tuple[str, str]] = []
    for baseline in baselines:
        if baseline.reference_state != "referenced_local":
            continue
        mapping.append((managed_reference(managed_path, baseline.local_path), baseline.filename))
    return tuple(sorted(mapping))


def _local_path(managed_path: Path, reference: str) -> Path | None:
    try:
        return resolve_managed_asset_path(managed_path, reference)
    except ValidationError:
        return None


def _conflict(
    reference: str,
    local_path: Path,
    remote_filename: str,
    *,
    baseline: AssetBaseline | None,
    remote: RemoteAttachmentSnapshot | None,
    reason: str,
    content_sha256: str | None = None,
) -> AssetPlanItem:
    return AssetPlanItem(
        reference=reference,
        local_path=local_path,
        remote_filename=remote_filename,
        content_sha256=content_sha256,
        action=AssetAction.CONFLICT,
        baseline=baseline,
        remote=remote,
        reason=reason,
    )


def build_asset_plan(
    *,
    managed_path: Path,
    references: Iterable[str],
    baselines: Iterable[AssetBaseline],
    remote: Iterable[RemoteAttachmentSnapshot],
    reference_remote_names: dict[str, str] | None = None,
) -> AssetPlan:
    """Classify referenced binaries without filename-only identity guesses."""

    baseline_items = tuple(baselines)
    remote_items = tuple(remote)
    baseline_by_path = {item.local_path.resolve(strict=False): item for item in baseline_items}
    baseline_by_name: dict[str, list[AssetBaseline]] = {}
    for item in baseline_items:
        baseline_by_name.setdefault(item.filename, []).append(item)
    remote_by_id = {item.attachment_id: item for item in remote_items}
    remote_by_name: dict[str, list[RemoteAttachmentSnapshot]] = {}
    for remote_item in remote_items:
        remote_by_name.setdefault(remote_item.filename, []).append(remote_item)

    items: list[AssetPlanItem] = []
    referenced_baselines: set[Path] = set()
    for reference in dict.fromkeys(references):
        local_path = _local_path(managed_path, reference)
        remote_filename = (reference_remote_names or {}).get(reference, PurePosixPath(reference).name)
        if local_path is None:
            items.append(
                _conflict(
                    reference,
                    managed_path.parent,
                    remote_filename,
                    baseline=None,
                    remote=None,
                    reason="unsafe_local_asset_path",
                )
            )
            continue
        baseline = baseline_by_path.get(local_path)
        if baseline is None and reference in (reference_remote_names or {}):
            candidates = baseline_by_name.get(remote_filename, [])
            if len(candidates) == 1:
                baseline = candidates[0]
        if baseline is not None:
            referenced_baselines.add(baseline.local_path.resolve(strict=False))
        if baseline is not None and baseline.reference_state == "remote_only":
            observed = remote_by_id.get(baseline.attachment_id or "")
            if observed is None:
                items.append(
                    _conflict(
                        reference,
                        local_path,
                        baseline.filename,
                        baseline=baseline,
                        remote=None,
                        reason="remote_attachment_missing",
                    )
                )
                continue
            if (
                observed.version != baseline.attachment_version
                or observed.content_sha256 != baseline.content_sha256
                or observed.filename != baseline.filename
            ):
                items.append(
                    _conflict(
                        reference,
                        local_path,
                        baseline.filename,
                        baseline=baseline,
                        remote=observed,
                        reason="remote_attachment_drift",
                    )
                )
                continue
            if local_path.exists() or local_path.is_symlink():
                items.append(
                    _conflict(
                        reference,
                        local_path,
                        baseline.filename,
                        baseline=baseline,
                        remote=observed,
                        reason="remote_only_local_collision",
                    )
                )
                continue
            items.append(
                AssetPlanItem(
                    reference,
                    local_path,
                    baseline.filename,
                    baseline.content_sha256,
                    AssetAction.UNCHANGED,
                    baseline,
                    observed,
                )
            )
            continue
        local_exists = local_path.is_file()
        desired_sha256: str | None = None
        local_file_identity: str | None = None
        if local_exists:
            try:
                local_file_identity = inspect_file_identity(local_path).key
                desired_sha256 = file_sha256(local_path)
            except OSError as error:
                raise ValidationError(
                    "Managed attachment could not be read during semantic preflight",
                    context={
                        "reason": "asset_preflight_io_failed",
                        "path": str(local_path),
                        "failure": type(error).__name__,
                    },
                ) from error

        if baseline is not None:
            observed = remote_by_id.get(baseline.attachment_id or "")
            if observed is None:
                items.append(
                    _conflict(
                        reference,
                        local_path,
                        baseline.filename,
                        baseline=baseline,
                        remote=None,
                        reason="remote_attachment_missing",
                        content_sha256=desired_sha256,
                    )
                )
                continue
            if (
                observed.version != baseline.attachment_version
                or observed.content_sha256 != baseline.content_sha256
                or observed.filename != baseline.filename
            ):
                items.append(
                    _conflict(
                        reference,
                        local_path,
                        baseline.filename,
                        baseline=baseline,
                        remote=observed,
                        reason="remote_attachment_drift",
                        content_sha256=desired_sha256,
                    )
                )
                continue
            if not local_exists:
                items.append(
                    _conflict(
                        reference,
                        local_path,
                        baseline.filename,
                        baseline=baseline,
                        remote=observed,
                        reason="local_asset_missing",
                    )
                )
                continue
            action = AssetAction.UNCHANGED if desired_sha256 == baseline.content_sha256 else AssetAction.NEW_VERSION
            items.append(
                AssetPlanItem(
                    reference,
                    local_path,
                    baseline.filename,
                    desired_sha256,
                    action,
                    baseline,
                    observed,
                    local_file_identity=local_file_identity,
                )
            )
            continue

        if not local_exists:
            items.append(
                _conflict(
                    reference,
                    local_path,
                    remote_filename,
                    baseline=None,
                    remote=None,
                    reason="local_asset_missing",
                )
            )
            continue
        same_name = remote_by_name.get(remote_filename, [])
        if same_name:
            exact = [item for item in same_name if item.content_sha256 == desired_sha256]
            if len(same_name) == 1 and len(exact) == 1:
                items.append(
                    AssetPlanItem(
                        reference,
                        local_path,
                        remote_filename,
                        desired_sha256,
                        AssetAction.UNCHANGED,
                        None,
                        exact[0],
                        reason="unique_existing_content_adopted",
                        local_file_identity=local_file_identity,
                    )
                )
            else:
                items.append(
                    _conflict(
                        reference,
                        local_path,
                        remote_filename,
                        baseline=None,
                        remote=same_name[0] if len(same_name) == 1 else None,
                        reason=(
                            "same_filename_different_content" if len(same_name) == 1 else "ambiguous_remote_filename"
                        ),
                        content_sha256=desired_sha256,
                    )
                )
            continue
        items.append(
            AssetPlanItem(
                reference,
                local_path,
                remote_filename,
                desired_sha256,
                AssetAction.NEW,
                local_file_identity=local_file_identity,
            )
        )

    for baseline in baseline_items:
        resolved = baseline.local_path.resolve(strict=False)
        if resolved in referenced_baselines:
            continue
        items.append(
            AssetPlanItem(
                reference="",
                local_path=resolved,
                remote_filename=baseline.filename,
                content_sha256=baseline.content_sha256,
                action=AssetAction.UNREFERENCED,
                baseline=baseline,
                remote=remote_by_id.get(baseline.attachment_id or ""),
            )
        )
    return AssetPlan(tuple(items))


def snapshot_remote_attachments(client: Any, page_id: str) -> tuple[RemoteAttachmentSnapshot, ...]:
    snapshots: list[RemoteAttachmentSnapshot] = []
    for attachment in client.list_attachments(page_id):
        download_link = attachment.links.download if attachment.links else None
        content = client.fetch_attachment_bytes(attachment.id, download_link)
        snapshots.append(
            RemoteAttachmentSnapshot(
                attachment_id=attachment.id,
                version=attachment_version(attachment),
                filename=attachment.title,
                content_sha256=hashlib.sha256(content).hexdigest(),
                size=len(content),
                media_type=attachment.media_type or mimetypes.guess_type(attachment.title)[0],
            )
        )
    return tuple(snapshots)


def reconcile_managed_asset_operation(
    asset: ManagedAssetOperation,
    remote: Iterable[RemoteAttachmentSnapshot],
    operation_assets: Iterable[ManagedAssetOperation] = (),
) -> ManagedAssetReconciliation:
    """Classify one portable receipt using exact attachment identity transitions."""

    observed = tuple(remote)
    by_id = [item for item in observed if item.attachment_id in {asset.baseline_id, asset.result_id}]
    if asset.status == "applied":
        exact = [
            item
            for item in by_id
            if item.attachment_id == asset.result_id
            and item.version == asset.result_version
            and item.filename == asset.remote_name
            and f"sha256:{item.content_sha256}" == asset.result_sha256
        ]
        if len(exact) == 1:
            return ManagedAssetReconciliation(asset)
        return ManagedAssetReconciliation(asset, reason="applied_asset_receipt_mismatch")

    if asset.action == "create":
        if asset.status == "upload_unknown":
            confirmed_predecessor_ids = {
                receipt.result_id
                for receipt in operation_assets
                if receipt.operation_id == asset.operation_id
                and receipt.request_ordinal < asset.request_ordinal
                and receipt.action == "create"
                and receipt.status == "applied"
                and receipt.result_id is not None
                and any(
                    item.attachment_id == receipt.result_id
                    and item.version == receipt.result_version
                    and item.filename == receipt.remote_name
                    and f"sha256:{item.content_sha256}" == receipt.result_sha256
                    for item in observed
                )
            }
            baseline_observed = tuple(item for item in observed if item.attachment_id not in confirmed_predecessor_ids)
            exact = [
                item
                for item in baseline_observed
                if item.version == 1
                and item.filename == asset.remote_name
                and f"sha256:{item.content_sha256}" == asset.local_sha256
            ]
            adoptable = [
                item
                for item in exact
                if attachment_inventory_sha256(
                    observed_item.attachment_id
                    for observed_item in baseline_observed
                    if observed_item.attachment_id != item.attachment_id
                )
                == asset.pre_upload_ids
            ]
            if len(adoptable) == 1:
                candidate = adoptable[0]
                return ManagedAssetReconciliation(
                    asset.transition(
                        "applied",
                        result_id=candidate.attachment_id,
                        result_version=candidate.version,
                        result_sha256=f"sha256:{candidate.content_sha256}",
                    ),
                    adopted_response_loss=True,
                )
            current_inventory = attachment_inventory_sha256(item.attachment_id for item in baseline_observed)
            if not exact and current_inventory == asset.pre_upload_ids:
                return ManagedAssetReconciliation(asset.transition("planned"), retry_upload=True)
            return ManagedAssetReconciliation(asset, reason="new_attachment_upload_outcome_unprovable")
        same_name = [item for item in observed if item.filename == asset.remote_name]
        if same_name:
            return ManagedAssetReconciliation(asset, reason="new_attachment_remote_name_conflict")
        return ManagedAssetReconciliation(asset, retry_upload=True)

    candidates = [item for item in observed if item.attachment_id == asset.baseline_id]
    if len(candidates) != 1:
        return ManagedAssetReconciliation(asset, reason="attachment_identity_ambiguous")
    candidate = candidates[0]
    baseline_exact = (
        candidate.version == asset.baseline_version
        and candidate.filename == asset.remote_name
        and f"sha256:{candidate.content_sha256}" == asset.baseline_sha256
    )
    if baseline_exact:
        if asset.action == "unchanged":
            return ManagedAssetReconciliation(
                asset.transition(
                    "applied",
                    result_id=candidate.attachment_id,
                    result_version=candidate.version,
                    result_sha256=f"sha256:{candidate.content_sha256}",
                )
                if asset.status != "applied"
                else asset
            )
        retry = asset.transition("planned") if asset.status == "upload_unknown" else asset
        return ManagedAssetReconciliation(retry, retry_upload=True)
    applied_exact = (
        asset.action == "update"
        and asset.baseline_version is not None
        and candidate.version == asset.baseline_version + 1
        and candidate.filename == asset.remote_name
        and f"sha256:{candidate.content_sha256}" == asset.local_sha256
    )
    if applied_exact and asset.status == "upload_unknown":
        return ManagedAssetReconciliation(
            asset.transition(
                "applied",
                result_id=candidate.attachment_id,
                result_version=candidate.version,
                result_sha256=f"sha256:{candidate.content_sha256}",
            ),
            adopted_response_loss=True,
        )
    return ManagedAssetReconciliation(asset, reason="attachment_version_or_hash_ambiguous")


def confirm_managed_create_response(
    payload: Mapping[str, Any],
    asset: ManagedAssetOperation,
    remote: Iterable[RemoteAttachmentSnapshot],
) -> ManagedAssetReconciliation:
    """Bind a same-process create response to one exact read-back attachment."""

    response: Mapping[str, Any] | None = payload
    results = payload.get("results")
    if isinstance(results, list):
        response = results[0] if len(results) == 1 and isinstance(results[0], Mapping) else None
    if response is None:
        return ManagedAssetReconciliation(asset, reason="new_attachment_response_identity_missing")
    attachment_id = response.get("id")
    if not isinstance(attachment_id, str) or not attachment_id:
        return ManagedAssetReconciliation(asset, reason="new_attachment_response_identity_missing")
    candidates = [item for item in remote if item.attachment_id == attachment_id]
    if len(candidates) != 1:
        return ManagedAssetReconciliation(asset, reason="new_attachment_response_identity_ambiguous")
    candidate = candidates[0]
    if candidate.filename != asset.remote_name or f"sha256:{candidate.content_sha256}" != asset.local_sha256:
        return ManagedAssetReconciliation(asset, reason="new_attachment_response_content_mismatch")
    return ManagedAssetReconciliation(
        asset.transition(
            "applied",
            result_id=candidate.attachment_id,
            result_version=candidate.version,
            result_sha256=f"sha256:{candidate.content_sha256}",
        )
    )


__all__ = [
    "AssetAction",
    "AssetBaseline",
    "AssetPlan",
    "AssetPlanItem",
    "ManagedAssetReference",
    "ManagedAssetReconciliation",
    "RemoteAttachmentSnapshot",
    "attachment_version",
    "attachment_filename_map",
    "bind_managed_attachment_markdown",
    "build_asset_plan",
    "confirm_managed_create_response",
    "file_sha256",
    "extract_managed_asset_references",
    "managed_reference",
    "reconcile_managed_asset_operation",
    "remote_id_set_sha256",
    "snapshot_remote_attachments",
    "rewrite_attachment_artifact",
    "rewrite_attachment_markdown",
]
