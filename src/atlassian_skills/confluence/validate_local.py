"""Portable managed Markdown validation without remote or global state."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from atlassian_skills.core.errors import ValidationError
from atlassian_skills.core.file_identity import inspect_file_identity
from atlassian_skills.core.managed_file import read_managed_utf8, resolve_managed_asset_path
from atlassian_skills.core.managed_manifest import (
    ManagedManifestError,
    canonical_asset_set_sha256,
    canonical_content_sha256,
    extract_asset_records,
    parse_managed_document,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def validate_local(path: Path) -> dict[str, Any]:
    """Validate one v2 manifest and local assets without opening state or contacting a server."""

    inspect_file_identity(path)
    markdown = read_managed_utf8(path, reason="managed_file_read_failed")
    records = extract_asset_records(markdown)
    try:
        document = parse_managed_document(markdown, assets=records, verify_content=False, verify_assets=False)
    except ManagedManifestError as error:
        reason = "legacy_manifest_repull_required" if "legacy" in error.reason else error.reason
        raise ValidationError(
            "Managed Markdown manifest is invalid", context={**error.context, "reason": reason}
        ) from error
    local_assets: list[dict[str, Any]] = []
    dirty_assets = 0
    for record in records:
        if record.materialization != "local":
            local_assets.append({"src": record.src, "materialization": record.materialization, "status": "remote_only"})
            continue
        asset_path = resolve_managed_asset_path(path, record.src)
        try:
            inspect_file_identity(asset_path)
            actual = _sha256(asset_path)
        except (OSError, ValidationError) as error:
            raise ValidationError(
                "Managed local asset is missing or unsafe",
                context={"reason": "managed_asset_invalid", "src": record.src},
            ) from error
        status = "unchanged" if actual == record.sha256 else "dirty"
        dirty_assets += status == "dirty"
        local_assets.append(
            {
                "src": record.src,
                "materialization": "local",
                "status": status,
                "expected_sha256": record.sha256,
                "actual_sha256": actual,
            }
        )
    content_sha256 = canonical_content_sha256(document.content)
    assets_sha256 = canonical_asset_set_sha256(records)
    return {
        "status": "local_valid",
        "path": str(path),
        "manifest": asdict(document.manifest),
        "body": {
            "dirty": content_sha256 != document.manifest.base_md,
            "content_sha256": content_sha256,
            "baseline_sha256": document.manifest.base_md,
        },
        "assets": {
            "dirty": dirty_assets,
            "records_sha256": assets_sha256,
            "manifest_sha256": document.manifest.assets,
            "items": local_assets,
        },
        "remote_freshness": "not_checked",
        "state_authority": False,
    }


__all__ = ["validate_local"]
