"""Verified client-side page copy for Confluence Server/Data Center."""

from __future__ import annotations

import hashlib
import html
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atlassian_skills.confluence.client import ConfluenceClient
from atlassian_skills.confluence.models import Attachment, PageVersion
from atlassian_skills.core.directory_capability import DirectoryCapability
from atlassian_skills.core.errors import AtlasError, ConflictError, ValidationError

DEFAULT_MAX_COPY_ATTACHMENT_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_COPY_ATTACHMENTS = 1_000

_NOT_COPIED = [
    "history",
    "comments",
    "labels",
    "restrictions",
    "likes",
    "watchers",
    "attachment_version_history",
]


@dataclass(frozen=True)
class _SourceSnapshot:
    page_id: str
    title: str
    version: int
    storage: str
    storage_sha256: str
    attachments: tuple[Attachment, ...]


@dataclass(frozen=True)
class _StagedAttachment:
    attachment: Attachment
    leaf: str
    path: Path
    file_identity: str
    sha256: str
    size: int


def _page_snapshot(client: ConfluenceClient, page_id: str, *, attachment_limit: int) -> _SourceSnapshot:
    raw = client.get_page_raw(page_id, expand="body.storage,version,space,history")
    try:
        title = str(raw["title"])
        version = int(raw["version"]["number"])
        storage = str(raw["body"]["storage"]["value"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValidationError(
            "Source page response is missing required copy fields",
            context={"reason": "invalid_source_page_response", "source_page_id": page_id},
        ) from error
    if raw.get("status") not in (None, "current"):
        raise ValidationError(
            "Only current Confluence pages can be copied",
            context={"reason": "source_page_not_current", "source_page_id": page_id},
        )
    attachments = tuple(client.list_attachments(page_id, limit=attachment_limit + 1))
    if len(attachments) > attachment_limit:
        raise ValidationError(
            "Source page has too many attachments for a bounded copy",
            context={
                "reason": "copy_attachment_count_limit_exceeded",
                "source_page_id": page_id,
                "limit": attachment_limit,
            },
        )
    titles = [attachment.title for attachment in attachments]
    identifiers = [attachment.id for attachment in attachments]
    if len(set(titles)) != len(titles) or len(set(identifiers)) != len(identifiers):
        raise ValidationError(
            "Source attachment identity is ambiguous",
            context={"reason": "ambiguous_source_attachment_identity", "source_page_id": page_id},
        )
    return _SourceSnapshot(
        page_id=page_id,
        title=title,
        version=version,
        storage=storage,
        storage_sha256=hashlib.sha256(storage.encode()).hexdigest(),
        attachments=attachments,
    )


def _attachment_version(attachment: Attachment) -> int | None:
    if isinstance(attachment.version, int):
        return attachment.version
    if isinstance(attachment.version, PageVersion):
        return attachment.version.number
    return None


def _attachment_fingerprint(attachment: Attachment) -> tuple[str, str, int | None, int | None]:
    return attachment.id, attachment.title, _attachment_version(attachment), attachment.file_size


def _assert_source_unchanged(before: _SourceSnapshot, after: _SourceSnapshot) -> None:
    if (
        before.version != after.version
        or before.storage_sha256 != after.storage_sha256
        or sorted(_attachment_fingerprint(item) for item in before.attachments)
        != sorted(_attachment_fingerprint(item) for item in after.attachments)
    ):
        raise ValidationError(
            "Source page changed during copy",
            context={
                "reason": "source_changed_during_copy",
                "source_page_id": before.page_id,
                "expected_version": before.version,
                "observed_version": after.version,
            },
        )


def _parent_space(client: ConfluenceClient, parent_id: str) -> str:
    raw = client.get_page_raw(parent_id, expand="space,version")
    try:
        return str(raw["space"]["key"])
    except (KeyError, TypeError) as error:
        raise ValidationError(
            "Destination parent response is missing its space",
            context={"reason": "invalid_destination_parent", "destination_parent_id": parent_id},
        ) from error


def _cql_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _title_candidate_ids(client: ConfluenceClient, *, space: str, title: str) -> tuple[str, ...]:
    result = client.search(
        f'space = "{_cql_literal(space)}" and type = page and title = "{_cql_literal(title)}"',
        limit=3,
    )
    return tuple(str(page.id) for page in result.results if page.title == title)


def _assert_title_available(client: ConfluenceClient, *, space: str, title: str) -> None:
    candidates = _title_candidate_ids(client, space=space, title=title)
    if candidates:
        raise ConflictError(
            "Destination space already contains a page with the requested copy title",
            context={
                "reason": "copy_destination_title_exists",
                "space": space,
                "title": title,
                "candidate_ids": list(candidates),
            },
            hint="Choose a unique run-scoped title and retry the dry-run before creating the copy.",
        )


def _stage_attachments(
    client: ConfluenceClient,
    snapshot: _SourceSnapshot,
    capability: DirectoryCapability,
    *,
    max_total_attachment_bytes: int,
) -> tuple[_StagedAttachment, ...]:
    declared_total = sum(attachment.file_size or 0 for attachment in snapshot.attachments)
    if declared_total > max_total_attachment_bytes:
        raise ValidationError(
            "Source attachments exceed the configured copy size limit",
            context={
                "reason": "copy_attachment_total_limit_exceeded",
                "declared_bytes": declared_total,
                "limit_bytes": max_total_attachment_bytes,
            },
        )
    staged: list[_StagedAttachment] = []
    actual_total = 0
    for index, attachment in enumerate(snapshot.attachments):
        download_link = attachment.links.download if attachment.links is not None else None
        content = client.fetch_attachment_bytes(attachment.id, download_link)
        actual_total += len(content)
        if actual_total > max_total_attachment_bytes:
            raise ValidationError(
                "Downloaded attachments exceed the configured copy size limit",
                context={
                    "reason": "copy_attachment_total_limit_exceeded",
                    "observed_bytes": actual_total,
                    "limit_bytes": max_total_attachment_bytes,
                },
            )
        if attachment.file_size is not None and len(content) != attachment.file_size:
            raise ValidationError(
                "Source attachment size changed during staging",
                context={
                    "reason": "source_attachment_size_mismatch",
                    "attachment_id": attachment.id,
                    "expected_bytes": attachment.file_size,
                    "observed_bytes": len(content),
                },
            )
        leaf = f"attachment-{index:05d}.bin"
        file_identity = capability.write_bytes_exclusive(leaf, content, mode=0o600)
        path = capability.path_for_leaf(leaf)
        staged.append(
            _StagedAttachment(
                attachment=attachment,
                leaf=leaf,
                path=path,
                file_identity=file_identity,
                sha256=hashlib.sha256(content).hexdigest(),
                size=len(content),
            )
        )
    return tuple(staged)


def _verify_target(
    client: ConfluenceClient,
    *,
    source: _SourceSnapshot,
    target_page_id: str,
    target_title: str,
    destination_parent_id: str,
    destination_space: str,
    staged: tuple[_StagedAttachment, ...],
    verify_bytes: bool,
    attachment_limit: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = client.get_page_raw(target_page_id, expand="body.storage,version,space,ancestors")
    try:
        observed_page_id = str(raw["id"])
        observed_title = str(raw["title"])
        target_page_version = int(raw["version"]["number"])
        target_space = str(raw["space"]["key"])
        target_storage = str(raw["body"]["storage"]["value"])
        ancestors = raw["ancestors"]
    except (KeyError, TypeError, ValueError) as error:
        raise ValidationError(
            "Copied page response is missing required verification fields",
            context={"reason": "invalid_copied_page_response", "target_page_id": target_page_id},
        ) from error
    target_storage_sha256 = hashlib.sha256(target_storage.encode()).hexdigest()
    if (
        observed_page_id != target_page_id
        or observed_title != target_title
        or target_space != destination_space
        or target_storage_sha256 != source.storage_sha256
        or target_page_version != 1
    ):
        raise ValidationError(
            "Copied page read-back does not match the requested baseline",
            context={
                "reason": "copied_page_readback_mismatch",
                "target_page_id": target_page_id,
                "observed_page_id": observed_page_id,
                "expected_title": target_title,
                "observed_title": observed_title,
                "target_space": target_space,
                "target_version": target_page_version,
                "expected_storage_sha256": source.storage_sha256,
                "observed_storage_sha256": target_storage_sha256,
                "storage_sha256_equal": target_storage_sha256 == source.storage_sha256,
            },
        )
    immediate_parent_id = None
    if isinstance(ancestors, list) and ancestors and isinstance(ancestors[-1], dict):
        immediate_parent_id = str(ancestors[-1].get("id", ""))
    if immediate_parent_id != destination_parent_id:
        raise ValidationError(
            "Copied page is not a child of the requested destination parent",
            context={
                "reason": "copied_page_parent_mismatch",
                "target_page_id": target_page_id,
                "expected_parent_id": destination_parent_id,
                "observed_parent_id": immediate_parent_id,
            },
        )

    target_attachments = client.list_attachments(target_page_id, limit=attachment_limit + 1)
    if len(target_attachments) > attachment_limit:
        raise ValidationError(
            "Copied page has too many attachments for bounded verification",
            context={
                "reason": "copied_attachment_count_limit_exceeded",
                "target_page_id": target_page_id,
                "limit": attachment_limit,
            },
        )
    by_title: dict[str, list[Attachment]] = {}
    for attachment in target_attachments:
        by_title.setdefault(attachment.title, []).append(attachment)
    if len(target_attachments) != len(staged):
        raise ValidationError(
            "Copied attachment count does not match the source",
            context={
                "reason": "copied_attachment_count_mismatch",
                "expected_count": len(staged),
                "observed_count": len(target_attachments),
            },
        )
    evidence: list[dict[str, Any]] = []
    for item in staged:
        matches = by_title.get(item.attachment.title, [])
        if len(matches) != 1:
            raise ValidationError(
                "Copied attachment title does not map uniquely",
                context={
                    "reason": "copied_attachment_identity_mismatch",
                    "source_attachment_id": item.attachment.id,
                },
            )
        target = matches[0]
        target_attachment_version = _attachment_version(target)
        if target.id == item.attachment.id or target_attachment_version != 1:
            raise ValidationError(
                "Copied attachment identity or version is invalid",
                context={
                    "reason": "copied_attachment_identity_mismatch",
                    "source_attachment_id": item.attachment.id,
                    "target_attachment_id": target.id,
                    "target_version": target_attachment_version,
                },
            )
        sha256_equal: bool | None = None
        if verify_bytes:
            download_link = target.links.download if target.links is not None else None
            target_content = client.fetch_attachment_bytes(target.id, download_link)
            sha256_equal = hashlib.sha256(target_content).hexdigest() == item.sha256
            if not sha256_equal:
                raise ValidationError(
                    "Copied attachment bytes do not match the source",
                    context={
                        "reason": "copied_attachment_hash_mismatch",
                        "source_attachment_id": item.attachment.id,
                        "target_attachment_id": target.id,
                    },
                )
        evidence.append(
            {
                "source_id": item.attachment.id,
                "target_id": target.id,
                "title": item.attachment.title,
                "source_version": _attachment_version(item.attachment),
                "target_version": target_attachment_version,
                "size": item.size,
                "sha256": item.sha256 if verify_bytes else None,
                "sha256_equal": sha256_equal,
            }
        )
    return (
        {
            "version": target_page_version,
            "title": observed_title,
            "storage_sha256": target_storage_sha256,
            "storage_sha256_equal": target_storage_sha256 == source.storage_sha256,
        },
        evidence,
    )


def copy_page(
    client: ConfluenceClient,
    source_page_id: str,
    *,
    destination_parent_id: str,
    destination_space: str,
    title: str | None = None,
    include_attachments: bool = False,
    verify: bool = True,
    reason: str | None = None,
    attachment_comment: str = "Copied by atls page copy",
    dry_run: bool = False,
    attachment_limit: int = DEFAULT_MAX_COPY_ATTACHMENTS,
    max_total_attachment_bytes: int = DEFAULT_MAX_COPY_ATTACHMENT_BYTES,
) -> dict[str, Any]:
    """Copy one current page and all attachments into a run-owned destination."""

    if source_page_id == destination_parent_id:
        raise ValidationError(
            "A page cannot be copied beneath itself",
            context={"reason": "copy_destination_is_source", "source_page_id": source_page_id},
        )
    source = _page_snapshot(client, source_page_id, attachment_limit=attachment_limit)
    parent_space = _parent_space(client, destination_parent_id)
    if parent_space != destination_space:
        raise ValidationError(
            "Destination parent is not in the requested space",
            context={
                "reason": "destination_parent_space_mismatch",
                "destination_parent_id": destination_parent_id,
                "expected_space": destination_space,
                "observed_space": parent_space,
            },
        )
    if source.attachments and not include_attachments:
        raise ValidationError(
            "Source page has attachments; use --include-attachments for a complete copy",
            context={
                "reason": "attachments_not_included",
                "source_page_id": source_page_id,
                "attachment_count": len(source.attachments),
            },
        )
    target_title = title or source.title
    _assert_title_available(client, space=destination_space, title=target_title)
    declared_total = sum(attachment.file_size or 0 for attachment in source.attachments)
    if dry_run:
        return {
            "status": "dry_run",
            "source": {"id": source.page_id, "version": source.version, "title": source.title},
            "target": {
                "space": destination_space,
                "parent_id": destination_parent_id,
                "title": target_title,
            },
            "attachments": {"count": len(source.attachments), "total_bytes": declared_total},
            "verify": verify,
            "not_copied": list(_NOT_COPIED),
        }

    target_page_id: str | None = None
    owned_target_page_id: str | None = None
    create_outcome = "response"
    try:
        with tempfile.TemporaryDirectory(prefix="atls-page-copy-") as temporary:
            root = Path(temporary)
            with DirectoryCapability.acquire(root) as capability:
                staged = (
                    _stage_attachments(
                        client,
                        source,
                        capability,
                        max_total_attachment_bytes=max_total_attachment_bytes,
                    )
                    if include_attachments
                    else ()
                )
                _assert_source_unchanged(
                    source,
                    _page_snapshot(client, source_page_id, attachment_limit=attachment_limit),
                )
                create_error: Exception | None = None
                try:
                    created = client.create_page(
                        destination_space,
                        target_title,
                        source.storage,
                        ancestor_id=destination_parent_id,
                        body_format="storage",
                    )
                    target_page_id = str(created.get("id", "")) if isinstance(created, dict) else ""
                except Exception as error:
                    create_error = error
                    target_page_id = None
                    create_outcome = "reconciled"

                candidates = _title_candidate_ids(client, space=destination_space, title=target_title)
                if target_page_id in {source_page_id, destination_parent_id}:
                    target_page_id = None
                if target_page_id and (not candidates or candidates == (target_page_id,)):
                    pass
                elif len(candidates) == 1:
                    target_page_id = candidates[0]
                    create_outcome = "reconciled"
                else:
                    target_page_id = None

                if target_page_id is None:
                    raise ValidationError(
                        "Page create outcome could not be proven after the non-retryable request",
                        context={
                            "reason": "page_copy_create_outcome_unknown",
                            "create_outcome": "unknown",
                            "space": destination_space,
                            "parent_id": destination_parent_id,
                            "title": target_title,
                            "candidate_ids": list(candidates),
                            "create_error": type(create_error).__name__ if create_error is not None else None,
                            "recovery": "Search the destination parent for this exact title and inspect version/storage before cleanup.",
                        },
                    ) from create_error
                if target_page_id in {source_page_id, destination_parent_id}:
                    raise ValidationError(
                        "Page create response points at a protected existing page",
                        context={
                            "reason": "invalid_page_copy_create_identity",
                            "target_page_id": target_page_id,
                            "source_page_id": source_page_id,
                            "destination_parent_id": destination_parent_id,
                        },
                    )

                page_evidence, _ = _verify_target(
                    client,
                    source=source,
                    target_page_id=target_page_id,
                    target_title=target_title,
                    destination_parent_id=destination_parent_id,
                    destination_space=destination_space,
                    staged=(),
                    verify_bytes=False,
                    attachment_limit=attachment_limit,
                )
                owned_target_page_id = target_page_id
                for item in staged:
                    with capability.open_readonly(
                        item.leaf,
                        expected_identity=item.file_identity,
                        expected_sha256=item.sha256,
                    ) as source_stream:
                        client.upload_attachment(
                            target_page_id,
                            item.path,
                            attachment_comment,
                            filename=item.attachment.title,
                            source_stream=source_stream,
                        )
                page_evidence, attachment_evidence = _verify_target(
                    client,
                    source=source,
                    target_page_id=target_page_id,
                    target_title=target_title,
                    destination_parent_id=destination_parent_id,
                    destination_space=destination_space,
                    staged=staged,
                    verify_bytes=verify,
                    attachment_limit=attachment_limit,
                )
                _assert_source_unchanged(
                    source,
                    _page_snapshot(client, source_page_id, attachment_limit=attachment_limit),
                )
                reason_comment_added = False
                if reason:
                    client.add_comment(target_page_id, f"<p>{html.escape(reason)}</p>")
                    reason_comment_added = True
                return {
                    "status": "copied",
                    "create_outcome": create_outcome,
                    "source": {
                        "id": source.page_id,
                        "version": source.version,
                        "storage_sha256": source.storage_sha256,
                    },
                    "target": {
                        "id": target_page_id,
                        "version": page_evidence["version"],
                        "space": destination_space,
                        "parent_id": destination_parent_id,
                        "title": page_evidence["title"],
                        "storage_sha256": page_evidence["storage_sha256"],
                    },
                    "storage": {"sha256_equal": page_evidence["storage_sha256_equal"]},
                    "attachments": {
                        "copied": len(staged),
                        "verified": sum(item["sha256_equal"] is True for item in attachment_evidence),
                        "items": attachment_evidence,
                    },
                    "source_unchanged": True,
                    "reason_comment_added": reason_comment_added,
                    "not_copied": list(_NOT_COPIED),
                }
    except Exception as error:
        if owned_target_page_id is not None:
            try:
                client.delete_page(owned_target_page_id)
            except Exception as cleanup_error:
                with suppress(Exception):
                    client.add_comment(
                        owned_target_page_id,
                        "<p>atls page copy failed and automatic cleanup also failed. Manual cleanup is required.</p>",
                    )
                raise ValidationError(
                    "Page copy failed and the destination could not be removed",
                    context={
                        "reason": "page_copy_cleanup_failed",
                        "target_page_id": owned_target_page_id,
                        "original_error": type(error).__name__,
                        "cleanup_error": type(cleanup_error).__name__,
                    },
                ) from error
        if isinstance(error, AtlasError):
            raise
        raise ValidationError(
            "Page copy failed",
            context={"reason": "page_copy_failed", "error_type": type(error).__name__},
        ) from error
