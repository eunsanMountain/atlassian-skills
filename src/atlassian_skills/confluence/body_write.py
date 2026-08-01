"""Durable Confluence body PUT intent and remote observation contracts."""

from __future__ import annotations

import hashlib
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import cfxmark

from atlassian_skills.confluence.asset_sync import (
    bind_managed_attachment_markdown,
    confirm_managed_create_response,
    reconcile_managed_asset_operation,
    rewrite_attachment_artifact,
    snapshot_remote_attachments,
)
from atlassian_skills.confluence.managed_operation import (
    ManagedAssetOperation,
    ManagedOperation,
    ManagedOperationError,
    asset_operation_token,
    finalize_managed_asset_operations,
    insert_managed_operation,
    managed_operation_authority,
    operation_for_preflight,
    parse_managed_asset_operations,
    parse_managed_operation,
    replace_managed_asset_operation,
    replace_managed_operation,
    stage_managed_asset_operations,
    strip_managed_operation,
)
from atlassian_skills.confluence.version_reason import proof_bound_version_reason
from atlassian_skills.core.attachment_io import atomic_write_bytes_bound, open_verified_attachment_snapshot
from atlassian_skills.core.directory_capability import DirectoryCapability
from atlassian_skills.core.errors import (
    MigrationConsentRequiredError,
    StaleError,
    ValidationError,
    consent_retry_action,
)
from atlassian_skills.core.managed_file import read_managed_utf8_bound
from atlassian_skills.core.managed_manifest import (
    CURRENT_MANAGED_MANIFEST_VERSION,
    ManagedManifest,
    ManagedManifestError,
    canonical_asset_set_sha256,
    canonical_content_sha256,
    canonical_managed_content,
    extract_asset_records,
    parse_passthrough,
    serialize_managed_manifest,
    serialize_passthrough,
    strip_managed_manifest,
)
from atlassian_skills.core.site_identity import site_fingerprint

if TYPE_CHECKING:
    from atlassian_skills.confluence.migration_preflight import ManagedPreflight


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _page_version(page: Any) -> int:
    version = getattr(page, "version", None)
    number = getattr(version, "number", None)
    if isinstance(number, int):
        return number
    if isinstance(version, int):
        return version
    raise ValidationError("Confluence page response has no usable version")


def _storage_equivalent(local_body: str, remote_body: str, *, passthrough: tuple[str, ...]) -> bool:
    if local_body == remote_body:
        return True
    from atlassian_skills.core.format.markdown import confluence_storage_to_md_result

    try:
        local = confluence_storage_to_md_result(
            local_body,
            profile="editable",
            passthrough_prefixes=passthrough,
        )
        remote = confluence_storage_to_md_result(
            remote_body,
            profile="editable",
            passthrough_prefixes=passthrough,
        )
    except (cfxmark.CfxmarkError, TypeError, ValueError):
        return False
    return bool(local.push_safe and remote.push_safe and local.markdown == remote.markdown)


def _remote_projection(page: Any, passthrough: Any) -> str | None:
    """`R2`: the readback storage projected back through the publishing converter.

    `None` when the projection cannot be trusted -- a conversion error, or output the
    converter will not vouch for. A baseline that cannot be reproduced is not a
    baseline, and guessing one here would still be recorded as proof. The caller keeps
    the submitted Markdown in that case, which is what shipped before this existed, so
    an unprojectable readback degrades a publish the server has already accepted rather
    than failing it.
    """

    from atlassian_skills.core.format.markdown import confluence_storage_to_md_result

    try:
        result = confluence_storage_to_md_result(
            page.body_storage or "",
            profile="editable",
            passthrough_prefixes=passthrough,
        )
    except (cfxmark.CfxmarkError, TypeError, ValueError):
        return None
    if not result.push_safe:
        return None
    return str(result.markdown)


def _identity_not_stored(preflight: ManagedPreflight, page: Any) -> dict[str, int]:
    """Which macro identities we sent the server is not holding afterwards.

    Asked of the readback rather than derived from the gate's pre-write answer, because
    they are different questions: the gate refuses a *candidate* that would drop an
    identity, and this reports a *server* that did not store one we sent. Nothing was
    asking the second, and nothing could -- `_readback_matches` compares the two sides
    as Markdown, which does not carry `ac:macro-id`.

    Append proofs are excluded. There the candidate is the remote plus a suffix and the
    macros in the prefix were never re-sent, so "we sent this id" is not true of them
    and a comparison would report the whole page as unstored.
    """

    from atlassian_skills.confluence.identity_gate import identity_not_stored

    if preflight.proof_mode == "exact_remote_prefix_append":
        return {}
    return identity_not_stored(preflight.candidate_storage, page.body_storage or "")


def _readback_matches(preflight: ManagedPreflight, page: Any) -> bool:
    if _page_version(page) != preflight.remote_version + 1:
        return False
    storage = page.body_storage or ""
    if preflight.proof_mode == "exact_remote_prefix_append":
        if not storage.startswith(preflight.source_storage):
            return False
        expected_suffix = preflight.candidate_storage[len(preflight.source_storage) :]
        remote_suffix = storage[len(preflight.source_storage) :]
        return _storage_equivalent(
            expected_suffix,
            remote_suffix,
            passthrough=preflight.document.manifest.passthrough,
        )
    return _storage_equivalent(
        preflight.candidate_storage,
        storage,
        passthrough=preflight.document.manifest.passthrough,
    )


def _validate_preflight_local_snapshot(
    preflight: ManagedPreflight,
    managed_path: Path,
    capability: DirectoryCapability,
) -> str:
    current = read_managed_utf8_bound(capability, managed_path)
    identity = capability.file_identity(managed_path.name)
    if identity != preflight.managed_file_identity or _sha256_text(current) != preflight.managed_file_sha256:
        raise ValidationError(
            "Managed Markdown changed after preflight",
            context={"reason": "local_changed_after_preflight"},
        )
    content, _manifest = strip_managed_manifest(current)
    assets = extract_asset_records(current)
    if (
        canonical_content_sha256(content) != preflight.managed_content_sha256
        or canonical_asset_set_sha256(assets) != preflight.managed_assets_sha256
    ):
        raise ValidationError(
            "Managed Markdown proof inputs changed after preflight",
            context={"reason": "local_changed_after_preflight"},
        )
    return current


def _revalidate_managed_directory(capability: DirectoryCapability) -> None:
    """Keep a managed operation bound to the directory selected at invocation time."""

    capability.revalidate()
    try:
        requested_now = capability.requested_directory.resolve(strict=True)
    except OSError as error:
        raise ValidationError(
            "Managed Markdown directory changed during publication",
            context={"reason": "managed_directory_changed"},
        ) from error
    if requested_now != capability.directory:
        raise ValidationError(
            "Managed Markdown directory changed during publication",
            context={
                "reason": "managed_directory_changed",
                "expected_directory": str(capability.directory),
                "observed_directory": str(requested_now),
            },
        )


def _validate_recovery_authority(
    client: Any,
    page_id: str,
    manifest: ManagedManifest,
    operation: ManagedOperation,
) -> None:
    effective_site_url = getattr(client, "base_url", None)
    if not isinstance(effective_site_url, str) or manifest.page != page_id:
        raise ValidationError(
            "Managed operation targets a different page or site",
            context={"reason": "managed_authority_mismatch"},
        )
    try:
        site = site_fingerprint(effective_site_url)
    except ValueError as error:
        raise ValidationError(
            "Managed operation targets a different page or site",
            context={"reason": "managed_authority_mismatch"},
        ) from error
    if manifest.site != site:
        raise ValidationError(
            "Managed operation targets a different page or site",
            context={"reason": "managed_authority_mismatch"},
        )
    if manifest.converter != f"cfxmark/{cfxmark.__version__}" or manifest.profile != "markdown-first":
        # Local import: migration_preflight is only reachable lazily from here
        # (see the same pattern for to_error_context below).
        from atlassian_skills.confluence.migration_preflight import MANAGED_CONVERTER_HINT

        raise ValidationError(
            "Managed operation converter/profile is not current",
            context={"reason": "managed_converter_mismatch"},
            hint=MANAGED_CONVERTER_HINT,
        )
    if (
        operation.authority != managed_operation_authority(manifest)
        or operation.source_version != manifest.remote_version
        or operation.source_storage != manifest.remote_storage
    ):
        raise ValidationError(
            "Managed operation is not bound to the manifest baseline",
            context={"reason": "operation_authority_mismatch"},
        )


def _validate_recovery_invocation(
    manifest: ManagedManifest,
    operation: ManagedOperation,
    *,
    passthrough_prefixes: tuple[str, ...] | None,
    if_version: int | None,
) -> None:
    if passthrough_prefixes is not None:
        try:
            supplied = parse_passthrough(serialize_passthrough(passthrough_prefixes))
        except ManagedManifestError as error:
            raise ValidationError(
                "--passthrough-prefix is invalid",
                context=error.context,
            ) from error
        if supplied != manifest.passthrough:
            raise ValidationError(
                "--passthrough-prefix does not match the pending managed operation",
                context={"reason": "passthrough_mismatch"},
            )
    if if_version is not None and if_version != operation.source_version:
        raise StaleError(
            f"Version mismatch: expected {if_version}, pending operation started from {operation.source_version}",
            context={
                "reason": "managed_if_version_mismatch",
                "expected_version": if_version,
                "operation_source_version": operation.source_version,
            },
        )


def _operation_matches_preflight(operation: ManagedOperation, preflight: ManagedPreflight) -> bool:
    expected = operation_for_preflight(preflight, operation_id=operation.operation_id)
    return replace(operation, stage="planned") == expected


def _require_migration_consent(
    preflight: ManagedPreflight,
    accept_migration: str | None,
    *,
    next_action_argv: tuple[str, ...] | None = None,
) -> None:
    if not preflight.consent_required:
        return
    if preflight.migration_fingerprint is None:
        raise ValidationError(
            "Consent-required preflight has no migration fingerprint",
            context={"reason": "migration_fingerprint_missing"},
        )
    if accept_migration != preflight.migration_fingerprint:
        context: dict[str, Any] = {
            "reason": "migration_consent_required",
            "migration_fingerprint": preflight.migration_fingerprint,
        }
        if next_action_argv is not None:
            from atlassian_skills.confluence.migration_preflight import to_error_context

            context = {
                **to_error_context(preflight.to_dict()),
                **context,
                "accepted": False,
                "next_actions": [
                    consent_retry_action(
                        next_action_argv,
                        option="--accept-migration",
                        fingerprint=preflight.migration_fingerprint,
                        description_code="REVIEW_MIGRATION_AND_RETRY",
                    )
                ],
            }
        raise MigrationConsentRequiredError(
            "Managed Markdown migration consent was not bound to the mutation",
            hint="Review the loss summary before running the returned command.",
            context=context,
        )


def _expected_append_equivalent(
    operation: ManagedOperation,
    manifest: ManagedManifest,
    content: str,
    page: Any,
) -> bool:
    if operation.proof != "exact_remote_prefix_append" or _page_version(page) != operation.expected_version:
        return False
    if operation.fragment_md_bytes is None or operation.append is None:
        return False
    storage_bytes = (page.body_storage or "").encode("utf-8")
    if len(storage_bytes) < operation.source_bytes:
        return False
    source_bytes = storage_bytes[: operation.source_bytes]
    suffix_bytes = storage_bytes[operation.source_bytes :]
    if "sha256:" + hashlib.sha256(source_bytes).hexdigest() != operation.source_storage:
        return False
    try:
        suffix = suffix_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return False
    edited_bytes = canonical_managed_content(content).encode("utf-8")
    if len(edited_bytes) <= operation.fragment_md_bytes:
        return False
    try:
        fragment = edited_bytes[-operation.fragment_md_bytes :].decode("utf-8")
    except UnicodeDecodeError:
        return False
    if _sha256_text(fragment) != operation.fragment_md:
        return False
    options = cfxmark.ConversionOptions(
        profile="editable",
        passthrough_html_comment_prefixes=manifest.passthrough,
    )
    try:
        fragment_artifact = cfxmark.to_cfx_artifact(fragment, options=options)
    except (cfxmark.CfxmarkError, TypeError, ValueError):
        return False
    if (
        not fragment_artifact.push_safe
        or fragment_artifact.attachments
        or fragment_artifact.presentation.cells
        or any(diagnostic.blocking for diagnostic in fragment_artifact.diagnostics)
    ):
        return False
    expected_suffix = fragment_artifact.xhtml
    if _sha256_text(expected_suffix) != operation.fragment_storage:
        return False
    intended_candidate = "sha256:" + hashlib.sha256(source_bytes + expected_suffix.encode("utf-8")).hexdigest()
    if intended_candidate != operation.candidate:
        return False
    from atlassian_skills.confluence.migration_preflight import append_proof_sha256

    if (
        append_proof_sha256(
            site=manifest.site,
            page_id=manifest.page,
            remote_version=manifest.remote_version,
            remote_storage_sha256=manifest.remote_storage,
            converter=manifest.converter,
            profile=manifest.profile,
            passthrough=manifest.passthrough,
            base_markdown_sha256=manifest.base_md,
            edited_markdown_sha256=operation.edited_md,
            fragment_markdown_sha256=operation.fragment_md,
            fragment_storage_sha256=operation.fragment_storage,
            candidate_storage_sha256=operation.candidate,
            asset_plan_sha256=operation.assets,
        )
        != operation.append
    ):
        return False
    return _storage_equivalent(expected_suffix, suffix, passthrough=manifest.passthrough)


def _expected_full_migration_equivalent(
    operation: ManagedOperation,
    manifest: ManagedManifest,
    content: str,
    assets: tuple[ManagedAssetOperation, ...],
    page: Any,
) -> bool:
    """Prove a full-migration readback through the same semantic projection as the normal write path."""

    if operation.proof != "full_migration" or _page_version(page) != operation.expected_version:
        return False
    filename_map = tuple((asset.src, asset.remote_name) for asset in assets if asset.src and asset.status == "applied")
    options = cfxmark.ConversionOptions(
        profile="editable",
        passthrough_html_comment_prefixes=manifest.passthrough,
        attachment_filename_map=filename_map,
    )
    remote_storage = page.body_storage or ""
    try:
        artifact = cfxmark.to_md_artifact(remote_storage, options=options)
        references = {asset.remote_name: asset.src for asset in assets}
        if references:
            artifact = rewrite_attachment_artifact(artifact, references)
        if not artifact.push_safe or any(diagnostic.blocking for diagnostic in artifact.diagnostics):
            return False
        edited = canonical_managed_content(content)
        if canonical_content_sha256(edited) != operation.edited_md:
            return False
        rebound = bind_managed_attachment_markdown(edited, dict(filename_map))
        candidate = cfxmark.to_cfx_artifact(
            rebound,
            presentation=artifact.presentation,
            options=options,
        )
    except (cfxmark.CfxmarkError, TypeError, ValueError):
        return False
    if not candidate.push_safe or any(diagnostic.blocking for diagnostic in candidate.diagnostics):
        return False
    return _storage_equivalent(candidate.xhtml, remote_storage, passthrough=manifest.passthrough)


def _operation_payload(
    operation: ManagedOperation,
    *,
    status: str,
    proof_mode: str | None = None,
    version: int | None = None,
    adopted_response_loss: bool = False,
    adopted_asset_response_loss: bool = False,
    asset_dirty: bool = False,
    reason: str | None = None,
    #: Present only on `published_normalized`: what the file said when it was submitted
    #: and what it says now. §9.10 requires the rewrite to be reported, and a receipt
    #: that only says the publish succeeded leaves the author's next diff unexplained.
    local_rewrite: dict[str, Any] | None = None,
    #: Per tracked attribute, how many macro identities we sent that the server is not
    #: holding afterwards. `{}` when the readback carries all of them, and `{}` rather
    #: than an absent key for the same reason as below: absent reads as "this build does
    #: not check", which is the state this was in.
    remote_reassigned_identity: dict[str, int] | None = None,
    #: How many places this publish replaced `<p/>` with `<p><br/></p>`, or `None`
    #: where this run cannot know.
    #:
    #: `None` is a third state on purpose, and it is not "no change". A recovery path
    #: reaches a receipt after somebody else's run already applied the operation, and
    #: all it holds of the pre-write page is a hash -- so the count is unavailable
    #: rather than zero. Reporting `false` there would be a claim, and omitting the
    #: field would be indistinguishable from a build that does not have it.
    presentation: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        # A reassignment is not a different kind of success. Every comment, permission
        # and attachment on those macros has detached, and an agent that reads `status`
        # and moves on would report the publish as clean -- so the word changes, the
        # way `published_normalized` changes it for a rewrite the author has to know
        # about.
        "status": (
            "reconciled_identity_reassigned" if status == "reconciled" and remote_reassigned_identity else status
        ),
        "remote_reassigned_identity": remote_reassigned_identity,
        "first_publish_changes_presentation": None if presentation is None else presentation > 0,
        "affected_occurrences": presentation,
        "operation_id": operation.operation_id,
        "proof_mode": proof_mode or operation.proof,
        "source_version": operation.source_version,
        "expected_version": operation.expected_version,
        "adopted_response_loss": adopted_response_loss,
        "adopted_asset_response_loss": adopted_asset_response_loss,
        "body_dirty": operation.candidate != operation.source_storage,
        "asset_dirty": asset_dirty,
    }
    if version is not None:
        payload["version"] = version
    if reason is not None:
        payload["reason"] = reason
    if local_rewrite is not None:
        payload["local_rewrite"] = local_rewrite
    action: dict[str, object] | None = None
    if status in {"readback_pending", "assets_applied_body_pending"}:
        action = {
            "id": "resume_operation",
            "requires_user_approval": False,
            "description_code": "RESUME_VERIFIED_OPERATION",
        }
    elif status in {"manual_recovery", "conflict"}:
        action = {
            "id": "inspect_operation",
            "requires_user_approval": False,
            "description_code": (
                "INSPECT_OPERATION_CONFLICT" if status == "conflict" else "INSPECT_OPERATION_RECOVERY"
            ),
        }
    elif status in {"manual_recovery_local_changed", "local_finalize_conflict"}:
        action = {
            "id": "inspect_operation",
            "requires_user_approval": False,
            "description_code": "INSPECT_LOCAL_RECOVERY_CONFLICT",
        }
    if action is not None:
        payload["next_actions"] = [action]
    return payload


def _dry_run_operation_payload(
    operation: ManagedOperation,
    *,
    status: str,
    version: int | None = None,
    asset_dirty: bool,
    reason: str | None = None,
    would_mutate: bool,
) -> dict[str, Any]:
    return {
        **_operation_payload(
            operation,
            status=status,
            version=version,
            asset_dirty=asset_dirty,
            reason=reason,
        ),
        "dry_run": True,
        "would_mutate": would_mutate,
    }


def _read_exact(capability: DirectoryCapability, path: Path) -> str:
    return read_managed_utf8_bound(capability, path)


def _write_exact(capability: DirectoryCapability, path: Path, content: str) -> None:
    atomic_write_bytes_bound(capability, path.name, content.encode("utf-8"))


def _transition_marker(
    capability: DirectoryCapability,
    path: Path,
    current_text: str,
    operation: ManagedOperation,
    stage: str,
) -> tuple[str, ManagedOperation] | None:
    observed = _read_exact(capability, path)
    if observed != current_text:
        return None
    if operation.stage == stage:
        # Re-emitting the current stage -- e.g. a second read-back failure while
        # recovering an operation already marked body_applied_readback_pending --
        # is idempotent, not an invalid self-transition.  Preserve the marker and
        # report the same stage instead of raising an uncaught ManagedOperationError.
        return current_text, operation
    desired = operation.transition(stage)
    updated = replace_managed_operation(current_text, desired)
    _write_exact(capability, path, updated)
    return updated, desired


def _finalize_manifest(
    capability: DirectoryCapability,
    path: Path,
    operation: ManagedOperation,
    page: Any,
    *,
    expected_marked_text: str | None,
    presentation: int | None = None,
    remote_reassigned_identity: dict[str, int] | None = None,
) -> dict[str, Any]:
    _revalidate_managed_directory(capability)
    current = _read_exact(capability, path)
    current_operation = parse_managed_operation(current)
    if current_operation is None or current_operation.operation_id != operation.operation_id:
        return _operation_payload(
            operation,
            presentation=presentation,
            remote_reassigned_identity=remote_reassigned_identity,
            status="local_finalize_conflict",
            version=_page_version(page),
            reason="operation_marker_changed",
        )
    if expected_marked_text is not None and current != expected_marked_text:
        return _operation_payload(
            operation,
            presentation=presentation,
            remote_reassigned_identity=remote_reassigned_identity,
            status="local_finalize_conflict",
            version=_page_version(page),
            reason="local_file_changed_before_finalize",
        )
    current_assets = parse_managed_asset_operations(current)
    from atlassian_skills.confluence.migration_preflight import rebuild_managed_asset_plan

    try:
        _asset_plan, asset_plan_sha256, _asset_dirty = rebuild_managed_asset_plan(path, current_assets)
    except ValidationError:
        return _operation_payload(
            operation,
            presentation=presentation,
            remote_reassigned_identity=remote_reassigned_identity,
            status="local_finalize_conflict",
            version=_page_version(page),
            asset_dirty=any(asset.action in {"create", "update"} for asset in current_assets),
            reason="local_asset_changed_before_finalize",
        )
    if asset_plan_sha256 != operation.assets:
        return _operation_payload(
            operation,
            presentation=presentation,
            remote_reassigned_identity=remote_reassigned_identity,
            status="local_finalize_conflict",
            version=_page_version(page),
            asset_dirty=any(asset.action in {"create", "update"} for asset in current_assets),
            reason="asset_operation_plan_changed_before_finalize",
        )
    without_operation = strip_managed_operation(current)
    promoted_assets = finalize_managed_asset_operations(without_operation, operation.operation_id)
    content, manifest = strip_managed_manifest(promoted_assets)
    if canonical_content_sha256(content) != operation.edited_md:
        return _operation_payload(
            operation,
            presentation=presentation,
            remote_reassigned_identity=remote_reassigned_identity,
            status="local_finalize_conflict",
            version=_page_version(page),
            reason="local_markdown_changed_before_finalize",
        )
    records = extract_asset_records(promoted_assets)
    remote_storage = page.body_storage or ""
    # §9.8/§9.9: the baseline is the readback projected through the same converter and
    # profile -- `R2` -- and not the Markdown that was submitted.
    #
    # They are usually the same string and sometimes not. The readback check passes on
    # semantic equivalence, so a server that stores our candidate and normalises its
    # representation still matches; projecting that storage back then yields Markdown
    # that differs from what was typed. Binding the submitted text made the manifest
    # describe a page the server is not holding, and the next compare reported a local
    # edit nobody made.
    # Asset-bearing documents included, which is the part U4 left open and review R3 found
    # independently (R3-3, blocker 12).
    #
    # A portable managed file points its image links at local asset paths; `R2` points them
    # at the remote attachment names, because that is what the storage says. They are
    # supposed to differ, so writing `R2` verbatim over a document with assets rewrote every
    # link back to a remote name and broke the portability the managed file exists for --
    # measured, which is how the earlier scope limit was found rather than reasoned.
    #
    # The answer is not to skip those documents. It is to apply the same rewrite the pull
    # applies, from the records the document is already carrying, so the projection names
    # the local assets and `base_md` describes the file that is actually on disk.
    projection = _remote_projection(page, manifest.passthrough)
    if projection is not None and records:
        from atlassian_skills.confluence.asset_sync import rewrite_projection_assets

        projection = rewrite_projection_assets(projection, records)
    normalized = projection is not None and canonical_content_sha256(projection) != operation.edited_md
    body = projection if normalized and projection is not None else content
    promoted = ManagedManifest(
        # §6.3: a document moves to v3 on the next SUCCESSFUL pull, push or record,
        # and only when its authority is Markdown and its baseline verified. Reaching
        # here is that condition met -- the PUT landed and the readback agreed -- so
        # the version moves with the baseline rather than on its own.
        v=CURRENT_MANAGED_MANIFEST_VERSION,
        page=manifest.page,
        site=manifest.site,
        remote_version=_page_version(page),
        remote_storage=_sha256_text(remote_storage),
        base_md=canonical_content_sha256(body),
        assets=canonical_asset_set_sha256(records),
        converter=manifest.converter,
        profile=manifest.profile,
        passthrough=manifest.passthrough,
    )
    final_text = serialize_managed_manifest(promoted) + "\n" + body
    _revalidate_managed_directory(capability)
    if _read_exact(capability, path) != current:
        return _operation_payload(
            operation,
            presentation=presentation,
            remote_reassigned_identity=remote_reassigned_identity,
            status="local_finalize_conflict",
            version=_page_version(page),
            reason="local_file_changed_before_finalize",
        )
    _write_exact(capability, path, final_text)
    if normalized:
        # §9.10. The publish succeeded *and* the file on disk is no longer what was
        # submitted. Reporting this as a plain success would leave the author's next
        # `git diff` showing changes they did not make, with nothing to attribute them
        # to. `published_normalized` is one of the five receipts §12.4 gives the
        # adapter, and this is the branch it exists for.
        return _operation_payload(
            operation,
            presentation=presentation,
            remote_reassigned_identity=remote_reassigned_identity,
            status="published_normalized",
            version=promoted.remote_version,
            asset_dirty=bool(parse_managed_asset_operations(current)),
            reason="remote_normalized_the_submitted_markdown",
            local_rewrite={
                "path": str(path),
                "submitted_md_sha256": operation.edited_md,
                "stored_md_sha256": promoted.base_md,
            },
        )
    return _operation_payload(
        operation,
        presentation=presentation,
        remote_reassigned_identity=remote_reassigned_identity,
        status="reconciled",
        version=promoted.remote_version,
        asset_dirty=bool(parse_managed_asset_operations(current)),
    )


def _replace_asset_marker(
    capability: DirectoryCapability,
    path: Path,
    expected_text: str,
    asset: ManagedAssetOperation,
) -> str | None:
    if _read_exact(capability, path) != expected_text:
        return None
    desired = replace_managed_asset_operation(expected_text, asset)
    _write_exact(capability, path, desired)
    return desired


def _manual_asset_recovery(
    capability: DirectoryCapability,
    path: Path,
    marked_text: str,
    operation: ManagedOperation,
    asset: ManagedAssetOperation,
    *,
    reason: str,
) -> dict[str, Any]:
    if _read_exact(capability, path) != marked_text:
        return _operation_payload(
            operation,
            status="manual_recovery_local_changed",
            asset_dirty=True,
            reason="local_file_changed_during_asset_recovery",
        )
    desired_asset = asset.transition("conflict") if asset.status in {"planned", "upload_unknown"} else asset
    desired_operation = (
        operation.transition("manual_recovery") if operation.stage not in {"manual_recovery", "conflict"} else operation
    )
    desired = replace_managed_asset_operation(marked_text, desired_asset)
    desired = replace_managed_operation(desired, desired_operation)
    _write_exact(capability, path, desired)
    return _operation_payload(
        desired_operation,
        status="manual_recovery",
        asset_dirty=True,
        reason=reason,
    )


def _verify_applied_assets(
    assets: tuple[ManagedAssetOperation, ...],
    remote: tuple[Any, ...],
) -> tuple[ManagedAssetOperation | None, str | None]:
    for asset in assets:
        result = reconcile_managed_asset_operation(asset, remote, assets)
        if result.reason is not None:
            return asset, result.reason
        if result.asset.status != "applied":
            return asset, "asset_receipt_not_applied"
    return None, None


def _publish_marked_operation(
    client: Any,
    preflight: ManagedPreflight,
    managed_path: Path,
    operation: ManagedOperation,
    marked_text: str,
    upload_streams: dict[str, Any],
    *,
    directory_capability: DirectoryCapability,
    accept_migration: str | None,
    next_action_argv: tuple[str, ...] | None,
    reason: str | None,
    minor_edit: bool,
) -> dict[str, Any]:
    _require_migration_consent(preflight, accept_migration, next_action_argv=next_action_argv)
    page = client.get_page(preflight.page_id)
    _revalidate_managed_directory(directory_capability)
    if not _source_matches(operation, page):
        raise StaleError(
            "Managed page changed immediately before the first remote mutation",
            context={"reason": "prewrite_remote_stale", "operation_id": operation.operation_id},
        )
    if _read_exact(directory_capability, managed_path) != marked_text:
        return _operation_payload(
            operation,
            status="manual_recovery_local_changed",
            asset_dirty=preflight.asset_dirty,
            reason="local_file_changed_before_remote_mutation",
        )

    assets = parse_managed_asset_operations(marked_text)
    item_by_src = {item.src: item for item in preflight.asset_plan if item.action != "unreferenced"}
    if set(item_by_src) != {asset.src for asset in assets}:
        return _operation_payload(
            operation,
            status="manual_recovery",
            asset_dirty=preflight.asset_dirty,
            reason="asset_operation_plan_mismatch",
        )
    remote = snapshot_remote_attachments(client, preflight.page_id) if assets else ()
    adopted_asset_response_loss = False
    for original_asset in assets:
        current_assets = parse_managed_asset_operations(marked_text)
        current_asset = next(
            asset for asset in current_assets if asset.request_ordinal == original_asset.request_ordinal
        )
        reconciliation = reconcile_managed_asset_operation(current_asset, remote, current_assets)
        adopted_asset_response_loss = adopted_asset_response_loss or reconciliation.adopted_response_loss
        if reconciliation.reason is not None:
            mutating_asset_applied = any(
                asset.action in {"create", "update"} and asset.status == "applied"
                for asset in parse_managed_asset_operations(marked_text)
            )
            if operation.stage == "planned" and current_asset.status != "upload_unknown" and not mutating_asset_applied:
                raise StaleError(
                    "Remote attachment state changed before upload",
                    context={
                        "reason": "prewrite_remote_asset_stale",
                        "src": current_asset.src,
                        "asset_reason": reconciliation.reason,
                    },
                )
            return _manual_asset_recovery(
                directory_capability,
                managed_path,
                marked_text,
                operation,
                current_asset,
                reason=reconciliation.reason,
            )
        if reconciliation.asset != current_asset:
            updated = _replace_asset_marker(directory_capability, managed_path, marked_text, reconciliation.asset)
            if updated is None:
                return _operation_payload(
                    operation,
                    status="manual_recovery_local_changed",
                    asset_dirty=preflight.asset_dirty,
                    reason="local_file_changed_during_asset_reconciliation",
                )
            marked_text = updated
            current_asset = reconciliation.asset
        if not reconciliation.retry_upload:
            continue

        item = item_by_src[current_asset.src]
        stream = upload_streams.get(current_asset.src)
        if stream is None:
            return _operation_payload(
                operation,
                status="manual_recovery_local_changed",
                asset_dirty=preflight.asset_dirty,
                reason="verified_asset_snapshot_missing",
            )
        upload_unknown = current_asset.transition("upload_unknown")
        updated = _replace_asset_marker(directory_capability, managed_path, marked_text, upload_unknown)
        if updated is None:
            return _operation_payload(
                operation,
                status="manual_recovery_local_changed",
                asset_dirty=preflight.asset_dirty,
                reason="local_file_changed_before_asset_upload",
            )
        marked_text = updated
        current_asset = upload_unknown
        stream.seek(0)
        _revalidate_managed_directory(directory_capability)
        upload_response: dict[str, Any] | None = None
        upload_error: Exception | None = None
        try:
            upload_response = client._upload_attachment_raw(
                preflight.page_id,
                item.local_path,
                comment=asset_operation_token(current_asset),
                filename=current_asset.remote_name,
                attachment_id=current_asset.baseline_id if current_asset.action == "update" else None,
                source_stream=stream,
            )
        except Exception as error:
            upload_error = error
        try:
            remote = snapshot_remote_attachments(client, preflight.page_id)
        except Exception:
            transitioned = _transition_marker(
                directory_capability,
                managed_path,
                marked_text,
                operation,
                "readback_pending",
            )
            if transitioned is not None:
                marked_text, operation = transitioned
            return _operation_payload(
                operation,
                status="readback_pending",
                asset_dirty=True,
                reason="asset_upload_readback_unavailable",
            )
        if current_asset.action == "create" and upload_response is not None:
            reconciliation = confirm_managed_create_response(upload_response, current_asset, remote)
        else:
            reconciliation = reconcile_managed_asset_operation(
                current_asset,
                remote,
                parse_managed_asset_operations(marked_text),
            )
        adopted_asset_response_loss = adopted_asset_response_loss or reconciliation.adopted_response_loss
        if reconciliation.reason is not None:
            return _manual_asset_recovery(
                directory_capability,
                managed_path,
                marked_text,
                operation,
                current_asset,
                reason=reconciliation.reason,
            )
        if reconciliation.asset.status != "applied":
            retry_marker = _replace_asset_marker(directory_capability, managed_path, marked_text, reconciliation.asset)
            if retry_marker is not None:
                marked_text = retry_marker
            return _operation_payload(
                operation,
                status="readback_pending",
                asset_dirty=True,
                reason="asset_upload_not_observed",
            )
        updated = _replace_asset_marker(directory_capability, managed_path, marked_text, reconciliation.asset)
        if updated is None:
            return _operation_payload(
                operation,
                status="local_finalize_conflict",
                asset_dirty=True,
                reason="local_file_changed_after_asset_upload",
            )
        marked_text = updated
        if upload_error is not None:
            adopted_asset_response_loss = True

    if operation.stage == "planned":
        transitioned = _transition_marker(
            directory_capability,
            managed_path,
            marked_text,
            operation,
            "assets_applied_body_pending",
        )
        if transitioned is None:
            return _operation_payload(
                operation,
                status="manual_recovery_local_changed",
                asset_dirty=preflight.asset_dirty,
                reason="local_file_changed_after_asset_execution",
            )
        marked_text, operation = transitioned

    if not preflight.body_dirty:
        readback = client.get_page(preflight.page_id)
        if not _source_matches(operation, readback):
            return _operation_payload(
                operation,
                status="conflict",
                asset_dirty=preflight.asset_dirty,
                reason="remote_body_changed_during_asset_only_operation",
            )
        remote = snapshot_remote_attachments(client, preflight.page_id) if assets else ()
        failed_asset, failed_reason = _verify_applied_assets(parse_managed_asset_operations(marked_text), remote)
        if failed_asset is not None:
            return _manual_asset_recovery(
                directory_capability,
                managed_path,
                marked_text,
                operation,
                failed_asset,
                reason=failed_reason or "asset_readback_mismatch",
            )
        result = _finalize_manifest(
            directory_capability,
            managed_path,
            operation,
            readback,
            expected_marked_text=marked_text,
            presentation=preflight.presentation_occurrences,
            remote_reassigned_identity=_identity_not_stored(preflight, readback),
        )
    else:
        result = _put_and_readback(
            client,
            preflight,
            managed_path,
            operation,
            marked_text,
            directory_capability=directory_capability,
            prewrite_page=page if not preflight.asset_dirty else None,
            reason=reason,
            minor_edit=minor_edit,
        )
    result["body_dirty"] = preflight.body_dirty
    result["asset_dirty"] = preflight.asset_dirty
    result["adopted_asset_response_loss"] = adopted_asset_response_loss
    return result


def _source_matches(operation: ManagedOperation, page: Any) -> bool:
    return (
        _page_version(page) == operation.source_version
        and _sha256_text(page.body_storage or "") == operation.source_storage
    )


def _expected_exact(operation: ManagedOperation, page: Any) -> bool:
    return (
        _page_version(page) == operation.expected_version
        and _sha256_text(page.body_storage or "") == operation.candidate
    )


def _put_and_readback(
    client: Any,
    preflight: ManagedPreflight,
    managed_path: Path,
    operation: ManagedOperation,
    marked_text: str,
    *,
    directory_capability: DirectoryCapability,
    prewrite_page: Any | None = None,
    reason: str | None,
    minor_edit: bool,
) -> dict[str, Any]:
    page = prewrite_page if prewrite_page is not None else client.get_page(preflight.page_id)
    _revalidate_managed_directory(directory_capability)
    if not _source_matches(operation, page):
        raise StaleError(
            "Managed page changed immediately before publish",
            context={"reason": "prewrite_remote_stale", "operation_id": operation.operation_id},
        )
    if _read_exact(directory_capability, managed_path) != marked_text:
        return _operation_payload(
            operation,
            status="manual_recovery_local_changed",
            reason="local_file_changed_before_put",
        )

    update_error: Exception | None = None
    proof_mode: Literal["exact_remote_prefix_append", "full_migration"]
    if preflight.proof_mode == "exact_remote_prefix_append":
        proof_mode = "exact_remote_prefix_append"
        proof_fingerprint = preflight.append_sha256
    elif preflight.proof_mode == "full_migration":
        proof_mode = "full_migration"
        proof_fingerprint = preflight.migration_fingerprint
    else:
        raise ValidationError(
            "Managed publication has no write proof for its version reason",
            context={"reason": "version_reason_proof_mode_invalid"},
        )
    if proof_fingerprint is None:
        raise ValidationError(
            "Managed publication has no proof fingerprint for its version reason",
            context={"reason": "version_reason_fingerprint_missing"},
        )
    version_options: dict[str, Any] = {
        "reason": proof_bound_version_reason(
            proof_mode=proof_mode,
            fingerprint=proof_fingerprint,
            migration_report=preflight.migration_report,
            user_reason=reason,
        )
    }
    if minor_edit:
        version_options["minor_edit"] = True
    _revalidate_managed_directory(directory_capability)
    try:
        client.update_page(
            page_id=preflight.page_id,
            title=page.title,
            body=preflight.candidate_storage,
            version_number=operation.expected_version,
            **version_options,
        )
    except Exception as error:  # response loss is reconciled through fresh remote evidence
        update_error = error

    try:
        readback = client.get_page(preflight.page_id)
    except Exception:
        transitioned = _transition_marker(
            directory_capability,
            managed_path,
            marked_text,
            operation,
            "body_applied_readback_pending",
        )
        if transitioned is not None:
            marked_text, operation = transitioned
        return _operation_payload(
            operation,
            status="readback_pending",
            reason="remote_readback_unavailable",
        )

    if _readback_matches(preflight, readback):
        assets = parse_managed_asset_operations(marked_text)
        if assets:
            remote_assets = snapshot_remote_attachments(client, preflight.page_id)
            failed_asset, failed_reason = _verify_applied_assets(assets, remote_assets)
            if failed_asset is not None:
                transitioned = _transition_marker(
                    directory_capability,
                    managed_path,
                    marked_text,
                    operation,
                    "readback_pending",
                )
                if transitioned is not None:
                    _marked, operation = transitioned
                return _operation_payload(
                    operation,
                    status="readback_pending",
                    version=_page_version(readback),
                    asset_dirty=preflight.asset_dirty,
                    reason=failed_reason or "asset_readback_mismatch",
                )
        finalized = _finalize_manifest(
            directory_capability,
            managed_path,
            operation,
            readback,
            expected_marked_text=marked_text,
            presentation=preflight.presentation_occurrences,
            remote_reassigned_identity=_identity_not_stored(preflight, readback),
        )
        if finalized["status"] == "reconciled" and update_error is not None:
            finalized["adopted_response_loss"] = True
        return finalized
    if _source_matches(operation, readback):
        if operation.stage == "planned":
            transitioned = _transition_marker(
                directory_capability,
                managed_path,
                marked_text,
                operation,
                "assets_applied_body_pending",
            )
            if transitioned is not None:
                _marked, operation = transitioned
        return _operation_payload(
            operation,
            status="assets_applied_body_pending",
            reason="body_put_not_observed",
        )
    if operation.proof == "exact_remote_prefix_append" and _page_version(readback) == operation.expected_version:
        transitioned = _transition_marker(
            directory_capability,
            managed_path,
            marked_text,
            operation,
            "manual_recovery",
        )
        if transitioned is not None:
            _marked, operation = transitioned
        return _operation_payload(
            operation,
            status="manual_recovery",
            version=_page_version(readback),
            asset_dirty=preflight.asset_dirty,
            reason="exact_append_remote_prefix_or_suffix_changed",
        )
    # We read the page, and what is on it is neither our candidate nor the source we
    # started from. That is not `readback_pending`.
    #
    # §9.12 splits the two on purpose: `readback_pending` means the readback did not
    # happen, so retrying it is the right move and recovery may adopt what it finds.
    # Here the readback happened and disagreed. Calling that "pending" understates
    # exactly the case where somebody else's edit is sitting on the page -- and it
    # sends recovery down a path that adopts remote state, which is the last thing to
    # do when the remote is holding a body nobody in this operation wrote.
    #
    # This is `body_put_failed` versus `body_put_not_observed` from U0, mirrored.
    # There a name claimed knowledge the code did not have; here it disclaimed
    # knowledge the code did have, and the reason string below has been saying so all
    # along.
    #
    # `manual_recovery` rather than a new `readback_mismatch`: §12.4 gives the adapter
    # five receipts to branch on and `readback_mismatch` is not one of them, so
    # introducing it would put a status in front of a consumer specified not to
    # understand it. The reason field is what distinguishes this from the append case.
    transitioned = _transition_marker(
        directory_capability,
        managed_path,
        marked_text,
        operation,
        "manual_recovery",
    )
    if transitioned is not None:
        _marked, operation = transitioned
    return _operation_payload(
        operation,
        status="manual_recovery",
        version=_page_version(readback),
        asset_dirty=preflight.asset_dirty,
        reason="remote_body_did_not_match_candidate",
    )


def publish_managed_body(
    client: Any,
    preflight: ManagedPreflight,
    managed_path: Path,
    *,
    accept_migration: str | None = None,
    next_action_argv: tuple[str, ...] | None = None,
    reason: str | None = None,
    minor_edit: bool = False,
) -> dict[str, Any]:
    """Journal, PUT, read back, and finalize a state-free managed body write."""

    _require_migration_consent(preflight, accept_migration, next_action_argv=next_action_argv)

    with ExitStack() as stack:
        directory_capability = stack.enter_context(DirectoryCapability.acquire(managed_path.parent))
        _revalidate_managed_directory(directory_capability)
        bound_path = directory_capability.path_for_leaf(managed_path.name)
        original = _validate_preflight_local_snapshot(preflight, bound_path, directory_capability)
        if parse_managed_operation(original) is not None:
            raise ValidationError(
                "Managed Markdown already contains a pending operation",
                context={"reason": "pending_operation_exists"},
            )
        operation = operation_for_preflight(preflight)
        upload_streams: dict[str, Any] = {}
        for item in preflight.asset_plan:
            if item.action not in {"create", "update"}:
                continue
            if item.local_file_identity is None:
                raise ValidationError(
                    "Managed attachment has no immutable local identity",
                    context={"reason": "asset_changed_after_preflight", "src": item.src},
                )
            upload_streams[item.src] = stack.enter_context(
                open_verified_attachment_snapshot(
                    item.local_path,
                    managed_root=bound_path.parent,
                    expected_identity=item.local_file_identity,
                    expected_sha256=item.current_sha256,
                    reference=item.src,
                )
            )
        if _validate_preflight_local_snapshot(preflight, bound_path, directory_capability) != original:
            raise ValidationError(
                "Managed Markdown changed after preflight",
                context={"reason": "local_changed_after_preflight"},
            )
        marked = insert_managed_operation(original, operation)
        marked = stage_managed_asset_operations(marked, operation, preflight.asset_plan)
        _write_exact(directory_capability, bound_path, marked)
        try:
            return _publish_marked_operation(
                client,
                preflight,
                bound_path,
                operation,
                marked,
                upload_streams,
                directory_capability=directory_capability,
                accept_migration=accept_migration,
                next_action_argv=next_action_argv,
                reason=reason,
                minor_edit=minor_edit,
            )
        except StaleError:
            if _read_exact(directory_capability, bound_path) == marked:
                _write_exact(directory_capability, bound_path, original)
            raise


def _recover_managed_body_bound(
    client: Any,
    page_id: str,
    managed_path: Path,
    *,
    directory_capability: DirectoryCapability,
    passthrough_prefixes: tuple[str, ...] | None = None,
    if_version: int | None = None,
    dry_run: bool = False,
    accept_migration: str | None = None,
    next_action_argv: tuple[str, ...] | None = None,
    reason: str | None = None,
    minor_edit: bool = False,
) -> dict[str, Any] | None:
    """Inspect or resume one durable body/asset operation before a new preflight."""

    current = _read_exact(directory_capability, managed_path)
    try:
        operation = parse_managed_operation(current)
        assets = parse_managed_asset_operations(current)
    except ManagedOperationError as error:
        raise ValidationError(
            "Managed operation journal is invalid",
            context={"reason": str(error)},
        ) from error
    if operation is None:
        if assets:
            raise ValidationError(
                "Managed asset receipts have no operation journal",
                context={"reason": "asset_operation_missing"},
            )
        return None
    asset_dirty = any(asset.action in {"create", "update"} for asset in assets)
    if any(asset.operation_id != operation.operation_id for asset in assets):
        raise ValidationError(
            "Managed asset receipts do not belong to the operation",
            context={"reason": "asset_operation_id_mismatch"},
        )
    if any(asset.operation_proof != operation.proof_bundle for asset in assets):
        raise ValidationError(
            "Managed asset receipts are not bound to the operation proof",
            context={"reason": "asset_operation_authority_mismatch"},
        )
    content, manifest = strip_managed_manifest(current)
    _validate_recovery_authority(client, page_id, manifest, operation)
    _validate_recovery_invocation(
        manifest,
        operation,
        passthrough_prefixes=passthrough_prefixes,
        if_version=if_version,
    )
    if canonical_content_sha256(content) != operation.edited_md:
        if dry_run:
            return _dry_run_operation_payload(
                operation,
                status="manual_recovery_local_changed",
                asset_dirty=asset_dirty,
                reason="local_markdown_changed_during_operation",
                would_mutate=False,
            )
        return _operation_payload(
            operation,
            status="manual_recovery_local_changed",
            asset_dirty=asset_dirty,
            reason="local_markdown_changed_during_operation",
        )
    if operation.stage in {"manual_recovery", "conflict"}:
        failed = next((asset for asset in assets if asset.status == "conflict"), None)
        failure_reason = (
            "new_attachment_upload_outcome_unprovable"
            if failed is not None and failed.action == "create"
            else "operation_requires_manual_recovery"
        )
        if dry_run:
            return _dry_run_operation_payload(
                operation,
                status=operation.stage,
                asset_dirty=asset_dirty,
                reason=failure_reason,
                would_mutate=False,
            )
        return _operation_payload(
            operation,
            status=operation.stage,
            asset_dirty=asset_dirty,
            reason=failure_reason,
        )

    from atlassian_skills.confluence.migration_preflight import (
        build_managed_preflight,
        rebuild_managed_asset_plan,
    )

    try:
        recovery_plan, recovery_plan_sha256, _recovery_dirty = rebuild_managed_asset_plan(managed_path, assets)
    except ValidationError as error:
        if dry_run:
            return _dry_run_operation_payload(
                operation,
                status="manual_recovery_local_changed",
                asset_dirty=asset_dirty,
                reason=str((error.context or {}).get("reason", "local_asset_changed_during_operation")),
                would_mutate=False,
            )
        return _operation_payload(
            operation,
            status="manual_recovery_local_changed",
            asset_dirty=asset_dirty,
            reason=str((error.context or {}).get("reason", "local_asset_changed_during_operation")),
        )
    if recovery_plan_sha256 != operation.assets:
        raise ValidationError(
            "Pending asset receipts do not match the operation plan",
            context={"reason": "asset_operation_plan_mismatch"},
        )

    remote = client.get_page(page_id)
    remote_assets = snapshot_remote_attachments(client, page_id) if assets else ()
    expected_applied = (
        _expected_exact(operation, remote)
        or _expected_append_equivalent(operation, manifest, content, remote)
        or _expected_full_migration_equivalent(operation, manifest, content, assets, remote)
    )
    if expected_applied:
        failed_asset, failed_reason = _verify_applied_assets(assets, remote_assets)
        if failed_asset is not None:
            if dry_run:
                return _dry_run_operation_payload(
                    operation,
                    status="readback_pending",
                    version=_page_version(remote),
                    asset_dirty=asset_dirty,
                    reason=failed_reason or "asset_readback_mismatch",
                    would_mutate=False,
                )
            return _operation_payload(
                operation,
                status="readback_pending",
                version=_page_version(remote),
                asset_dirty=asset_dirty,
                reason=failed_reason or "asset_readback_mismatch",
            )
        if dry_run:
            return _dry_run_operation_payload(
                operation,
                status="reconciled",
                version=_page_version(remote),
                asset_dirty=asset_dirty,
                reason="pending_operation_remote_result_is_equivalent",
                would_mutate=True,
            )
        _revalidate_managed_directory(directory_capability)
        return _finalize_manifest(
            directory_capability,
            managed_path,
            operation,
            remote,
            expected_marked_text=None,
        )
    recovery_preflight: ManagedPreflight | None = None
    if _source_matches(operation, remote):
        recovery_preflight = build_managed_preflight(
            client,
            page_id,
            managed_path,
            recovery_operation=operation,
            recovery_assets=assets,
        )
        if not _operation_matches_preflight(operation, recovery_preflight):
            if dry_run:
                return _dry_run_operation_payload(
                    operation,
                    status="manual_recovery",
                    asset_dirty=asset_dirty,
                    reason="operation_preflight_no_longer_reproducible",
                    would_mutate=False,
                )
            return _operation_payload(
                operation,
                status="manual_recovery",
                asset_dirty=asset_dirty,
                reason="operation_preflight_no_longer_reproducible",
            )
    else:
        if _page_version(remote) == operation.expected_version and operation.proof == "exact_remote_prefix_append":
            desired_stage = "manual_recovery"
            reason_code = "exact_append_remote_prefix_or_suffix_changed"
        elif _page_version(remote) == operation.expected_version and operation.stage in {
            "body_applied_readback_pending",
            "readback_pending",
        }:
            desired_stage = "readback_pending"
            reason_code = "remote_candidate_readback_incomplete"
        else:
            desired_stage = "conflict"
            reason_code = "remote_matches_neither_source_nor_exact_candidate"
        if desired_stage != operation.stage:
            if not dry_run:
                transitioned = _transition_marker(
                    directory_capability,
                    managed_path,
                    current,
                    operation,
                    desired_stage,
                )
                if transitioned is not None:
                    current, operation = transitioned
        if dry_run:
            return _dry_run_operation_payload(
                operation,
                status=desired_stage,
                version=_page_version(remote),
                asset_dirty=asset_dirty,
                reason=reason_code,
                would_mutate=desired_stage != operation.stage,
            )
        return _operation_payload(
            operation,
            status=desired_stage,
            version=_page_version(remote),
            asset_dirty=asset_dirty,
            reason=reason_code,
        )

    failed_asset, failed_reason = _verify_applied_assets(
        tuple(asset for asset in assets if asset.status == "applied"),
        remote_assets,
    )
    if failed_asset is not None:
        if dry_run:
            return _dry_run_operation_payload(
                operation,
                status="manual_recovery",
                asset_dirty=asset_dirty,
                reason=failed_reason or "asset_readback_mismatch",
                would_mutate=False,
            )
        return _operation_payload(
            operation,
            status="manual_recovery",
            asset_dirty=asset_dirty,
            reason=failed_reason or "asset_readback_mismatch",
        )
    if operation.candidate == operation.source_storage and all(asset.status == "applied" for asset in assets):
        if dry_run:
            return _dry_run_operation_payload(
                operation,
                status="reconciled",
                version=_page_version(remote),
                asset_dirty=asset_dirty,
                reason="pending_asset_only_operation_is_exact",
                would_mutate=True,
            )
        _revalidate_managed_directory(directory_capability)
        return _finalize_manifest(
            directory_capability,
            managed_path,
            operation,
            remote,
            expected_marked_text=None,
        )

    assert recovery_preflight is not None
    preflight = recovery_preflight
    if dry_run:
        return _dry_run_operation_payload(
            operation,
            status=operation.status.value,
            version=_page_version(remote),
            asset_dirty=asset_dirty,
            reason="pending_operation_requires_recovery",
            would_mutate=True,
        )
    with ExitStack() as stack:
        upload_streams: dict[str, Any] = {}
        asset_by_src = {asset.src: asset for asset in assets}
        for item in recovery_plan:
            receipt = asset_by_src[item.src]
            if item.action not in {"create", "update"} or receipt.status == "applied":
                continue
            if item.local_file_identity is None:
                return _operation_payload(
                    operation,
                    status="manual_recovery_local_changed",
                    asset_dirty=asset_dirty,
                    reason="asset_identity_missing_during_recovery",
                )
            upload_streams[item.src] = stack.enter_context(
                open_verified_attachment_snapshot(
                    item.local_path,
                    managed_root=managed_path.parent,
                    expected_identity=item.local_file_identity,
                    expected_sha256=item.current_sha256,
                    reference=item.src,
                )
            )
        return _publish_marked_operation(
            client,
            preflight,
            managed_path,
            operation,
            current,
            upload_streams,
            directory_capability=directory_capability,
            accept_migration=accept_migration,
            next_action_argv=next_action_argv,
            reason=reason,
            minor_edit=minor_edit,
        )


def recover_managed_body(
    client: Any,
    page_id: str,
    managed_path: Path,
    *,
    passthrough_prefixes: tuple[str, ...] | None = None,
    if_version: int | None = None,
    dry_run: bool = False,
    accept_migration: str | None = None,
    next_action_argv: tuple[str, ...] | None = None,
    reason: str | None = None,
    minor_edit: bool = False,
) -> dict[str, Any] | None:
    """Inspect or resume one durable operation through a stable managed-directory capability."""

    with DirectoryCapability.acquire(managed_path.parent) as directory_capability:
        _revalidate_managed_directory(directory_capability)
        bound_path = directory_capability.path_for_leaf(managed_path.name)
        return _recover_managed_body_bound(
            client,
            page_id,
            bound_path,
            directory_capability=directory_capability,
            passthrough_prefixes=passthrough_prefixes,
            if_version=if_version,
            dry_run=dry_run,
            accept_migration=accept_migration,
            next_action_argv=next_action_argv,
            reason=reason,
            minor_edit=minor_edit,
        )


__all__ = [
    "publish_managed_body",
    "recover_managed_body",
]
