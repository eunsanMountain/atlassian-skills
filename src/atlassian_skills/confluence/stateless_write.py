"""State-free, source-bound Confluence page create and update workflows."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from typing import Any
from urllib.parse import urlsplit

import cfxmark
from cfxmark.ast import Image

from atlassian_skills.confluence.migration_preflight import (
    OWNERSHIP_PROOF_HINT,
    _canonical_json,
    _consent_required,
    _migration_report_payload,
    _ownership_payload,
    _report_hash,
    _sha256_bytes,
    conversion_failure_context,
    ownership_error_context,
    storage_comparison_context,
    to_error_context,
)
from atlassian_skills.confluence.page_copy import _title_candidate_ids
from atlassian_skills.confluence.version_reason import proof_bound_version_reason
from atlassian_skills.core.errors import (
    ConflictError,
    ConversionConsentRequiredError,
    MigrationConsentRequiredError,
    StaleError,
    ValidationError,
    consent_retry_action,
)
from atlassian_skills.core.site_identity import site_fingerprint

_EMPTY_MIGRATION_REPORT: dict[str, Any] = {
    "schema": "cfxmark-migration-report-v1",
    "occurrences": [],
}
_EMPTY_OWNERSHIP: dict[str, Any] = {
    "intended_operation_ids": [],
    "accepted_migration_occurrence_ids": [],
    "unclassified": [],
    "multiple_owners": [],
    "overlap": [],
    "fatal_diagnostic_codes": [],
}


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _candidate_sha256(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _page_version(page: Any) -> int:
    version = getattr(page, "version", None)
    return int(getattr(version, "number", version) or 1)


def _walk_values(value: object) -> Iterator[object]:
    yield value
    if is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            yield from _walk_values(getattr(value, field.name))
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _walk_values(item)


def _image_source_classification(image: Image) -> dict[str, Any]:
    """Classify an image source value-free: scheme, credential presence, managed reference.

    Never returns the URL, host, path, userinfo, or attachment filename.
    """

    parsed = urlsplit(image.src)
    return {
        "scheme": parsed.scheme.lower() or None,
        "credential_present": parsed.username is not None or parsed.password is not None,
        "local_managed": image.attachment_filename is not None,
    }


def _validate_stateless_images(markdown: str, document: object) -> int:
    if "<!-- cfxmark:asset" in markdown or "<!-- atls:managed" in markdown:
        raise ValidationError(
            "Managed Markdown assets require pull-md/push-md",
            context={"reason": "managed_asset_requires_push_md"},
        )
    count = 0
    for value in _walk_values(document):
        if not isinstance(value, Image):
            continue
        count += 1
        parsed = urlsplit(value.src)
        if (
            value.attachment_filename is not None
            or parsed.scheme.lower() != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValidationError(
                "Stateless Markdown writes allow only credential-free absolute HTTPS image URLs",
                hint="Use pull-md/push-md for local or managed attachments.",
                context={
                    "reason": "stateless_image_source_unsupported",
                    "source_classification": _image_source_classification(value),
                    "asset_sync": False,
                },
            )
    return count


def _diagnostic_payload(diagnostic: Any, *, display: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "occurrence_id": diagnostic.occurrence_id,
        "code": diagnostic.code,
        "category": diagnostic.category,
        "severity": diagnostic.severity,
        "location": asdict(diagnostic.location),
        "resolutions": [asdict(item) for item in diagnostic.resolutions],
    }
    if display:
        payload["message"] = diagnostic.message
    return payload


def _source_conversion_report(diagnostics: tuple[Any, ...], *, display: bool) -> dict[str, Any]:
    occurrences = [_diagnostic_payload(item, display=display) for item in diagnostics]
    occurrences.sort(key=lambda item: (item["code"], item["occurrence_id"]))
    return {"schema": "atls-source-conversion-report-v1", "occurrences": occurrences}


def _validate_source_diagnostics(diagnostics: tuple[Any, ...]) -> None:
    invalid = [
        item.code
        for item in diagnostics
        if item.category != "content_loss"
        or item.severity != "blocking"
        or not isinstance(item.occurrence_id, str)
        or not item.occurrence_id
    ]
    if invalid:
        raise ValidationError(
            "Source conversion produced an unclassified or fatal diagnostic",
            context={
                "reason": "source_conversion_unclassified",
                "diagnostic_codes": sorted(set(invalid)),
            },
        )


@dataclass(frozen=True)
class SourceConversion:
    markdown_sha256: str
    candidate_storage: str
    candidate_storage_sha256: str
    report: dict[str, Any]
    report_sha256: str
    consent_required: bool
    conversion_fingerprint: str | None
    external_image_count: int
    warnings: tuple[str, ...]
    artifact: cfxmark.CfxArtifact

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_markdown_sha256": self.markdown_sha256,
            "candidate_storage_sha256": self.candidate_storage_sha256,
            "source_conversion_report": self.report,
            "source_conversion_report_sha256": self.report_sha256,
            "conversion_fingerprint": self.conversion_fingerprint,
            "consent_required": self.consent_required,
            "asset_sync": False,
            "external_images": {
                "count": self.external_image_count,
                "availability_verified": False,
            },
            "conversion": {
                "push_safe": not self.consent_required,
                "warnings": list(self.warnings),
                "losses": [item["message"] for item in self.report["occurrences"]],
            },
        }


def build_source_conversion(markdown: str) -> SourceConversion:
    """Build a classified Markdown-to-storage conversion proof for page create."""

    from atlassian_skills.confluence.push_md import _assert_push_safe_source
    from atlassian_skills.core.format.markdown import _line_ending_warnings

    _assert_push_safe_source(markdown)
    try:
        artifact = cfxmark.to_cfx_artifact(markdown)
        cfxmark.validate_cfx_artifact(artifact)
    except (cfxmark.CfxmarkError, TypeError, ValueError) as error:
        raise ValidationError(
            "Markdown did not produce a valid Confluence storage candidate",
            context={
                "reason": "source_conversion_candidate_invalid",
                **conversion_failure_context(error),
            },
        ) from error
    diagnostics = tuple(artifact.diagnostics)
    if not artifact.push_safe and not diagnostics:
        raise ValidationError(
            "Source conversion is not publishable and did not classify why",
            context={"reason": "source_conversion_unclassified", "diagnostic_codes": []},
        )
    _validate_source_diagnostics(diagnostics)
    external_image_count = _validate_stateless_images(markdown, artifact.document)
    report = _source_conversion_report(diagnostics, display=True)
    report_sha256 = _sha256_bytes(_canonical_json(_source_conversion_report(diagnostics, display=False)))
    consent_required = bool(diagnostics) or not artifact.push_safe
    candidate_storage_sha256 = _candidate_sha256(artifact.xhtml)
    fingerprint: str | None = None
    if consent_required:
        fingerprint = _sha256_bytes(
            _canonical_json(
                {
                    "schema": "atls-source-conversion-v1",
                    "converter": f"cfxmark/{cfxmark.__version__}",
                    "profile": "markdown-first",
                    "source_markdown_sha256": _sha256_text(markdown),
                    "candidate_storage_sha256": candidate_storage_sha256,
                    "source_conversion_report_sha256": report_sha256,
                }
            ),
            prefix="conv_sha256",
        )
    return SourceConversion(
        markdown_sha256=_sha256_text(markdown),
        candidate_storage=artifact.xhtml,
        candidate_storage_sha256=candidate_storage_sha256,
        report=report,
        report_sha256=report_sha256,
        consent_required=consent_required,
        conversion_fingerprint=fingerprint,
        external_image_count=external_image_count,
        warnings=_line_ending_warnings(markdown),
        artifact=artifact,
    )


@dataclass(frozen=True)
class PageUpdatePreflight:
    page_id: str
    body_format: str
    remote_title: str
    title: str
    remote_version: int
    remote_storage: str
    remote_storage_sha256: str
    candidate_storage: str
    candidate_storage_sha256: str
    migration_report: dict[str, Any]
    migration_report_sha256: str
    source_conversion_report: dict[str, Any]
    source_conversion_report_sha256: str
    migration_fingerprint: str | None
    consent_required: bool
    ownership: dict[str, Any]
    external_image_count: int

    @property
    def would_update(self) -> bool:
        # A candidate that differs from the remote only by the server-added macro
        # envelope (ac:macro-id / ac:schema-version="1" the remote already carries)
        # is a proven no-op, so skip the PUT. Every real content, structure,
        # parameter, or candidate-owned id/schema change still requires the update.
        return (
            self.title != self.remote_title
            or not cfxmark.storage_noop_comparison(self.candidate_storage, self.remote_storage).equivalent
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "migration_consent_required" if self.consent_required else "ready_to_update",
            "page_id": self.page_id,
            "body_format": self.body_format,
            "title_dirty": self.title != self.remote_title,
            "remote_version": self.remote_version,
            "remote_storage_sha256": self.remote_storage_sha256,
            "candidate_storage_sha256": self.candidate_storage_sha256,
            "would_update": self.would_update,
            "migration_report": self.migration_report,
            "migration_report_sha256": self.migration_report_sha256,
            "source_conversion_report": self.source_conversion_report,
            "source_conversion_report_sha256": self.source_conversion_report_sha256,
            "migration_fingerprint": self.migration_fingerprint,
            "consent_required": self.consent_required,
            "ownership": self.ownership,
            "asset_sync": False,
            "external_images": {
                "count": self.external_image_count,
                "availability_verified": False,
            },
        }


def _build_markdown_update(
    *,
    page_id: str,
    site: str,
    remote_version: int,
    remote_storage: str,
    markdown: str,
    remote_title: str,
    title: str,
) -> PageUpdatePreflight:
    from atlassian_skills.confluence.push_md import _assert_push_safe_source

    _assert_push_safe_source(markdown)
    options = cfxmark.ConversionOptions(profile="editable")
    try:
        base = cfxmark.to_md_artifact(remote_storage, options=options)
        migration_base = replace(base, protected_regions=(), remote_subtrees=())
        candidate = cfxmark.to_cfx_artifact(
            markdown,
            presentation=migration_base.presentation,
            base_artifact=migration_base,
            splice_source=remote_storage,
            options=options,
        )
        cfxmark.validate_managed_cfx_artifact(
            candidate,
            source_storage=remote_storage,
            options=options,
        )
    except cfxmark.OwnershipProofError as error:
        raise ValidationError(
            "Markdown update has no complete source-bound ownership proof",
            context=ownership_error_context(error.summary, reason="ownership_proof_invalid"),
            hint=OWNERSHIP_PROOF_HINT,
        ) from error
    except (cfxmark.CfxmarkError, TypeError, ValueError) as error:
        raise ValidationError(
            "Markdown update has no complete source-bound ownership proof",
            context={"reason": "ownership_proof_invalid", **conversion_failure_context(error)},
            hint=OWNERSHIP_PROOF_HINT,
        ) from error
    ownership = _ownership_payload(candidate)
    if any(ownership[key] for key in ("unclassified", "multiple_owners", "overlap", "fatal_diagnostic_codes")):
        raise ValidationError(
            "Markdown update ownership is incomplete or ambiguous",
            context=ownership_error_context(ownership, reason="ownership_proof_fatal"),
            hint=OWNERSHIP_PROOF_HINT,
        )
    diagnostics = tuple(candidate.diagnostics)
    if not candidate.push_safe and not diagnostics:
        raise ValidationError(
            "Markdown update is not publishable and did not classify why",
            context={"reason": "source_conversion_unclassified", "diagnostic_codes": []},
        )
    _validate_source_diagnostics(diagnostics)
    external_image_count = _validate_stateless_images(markdown, candidate.document)
    source_report = _source_conversion_report(diagnostics, display=True)
    source_report_sha256 = _sha256_bytes(_canonical_json(_source_conversion_report(diagnostics, display=False)))
    report = candidate.source_migration_report or base.migration_report
    accepted_ids = frozenset(ownership["accepted_migration_occurrence_ids"])
    report_payload = _migration_report_payload(report, display=True, occurrence_ids=accepted_ids)
    report_sha256 = _report_hash(report, occurrence_ids=accepted_ids)
    consent_required = (
        _consent_required(report, occurrence_ids=accepted_ids) or bool(diagnostics) or not candidate.push_safe
    )
    candidate_storage_sha256 = _candidate_sha256(candidate.xhtml)
    fingerprint = _sha256_bytes(
        _canonical_json(
            {
                "schema": "atls-page-update-migration-v1",
                "site": site,
                "page": page_id,
                "remote_version": remote_version,
                "remote_storage_sha256": _sha256_text(remote_storage),
                "remote_title_sha256": _sha256_text(remote_title),
                "target_title_sha256": _sha256_text(title),
                "converter": f"cfxmark/{cfxmark.__version__}",
                "profile": "markdown-first",
                "edited_markdown_sha256": _sha256_text(markdown),
                "candidate_storage_sha256": candidate_storage_sha256,
                "migration_report_sha256": report_sha256,
                "source_conversion_report_sha256": source_report_sha256,
                "ownership_proof_sha256": candidate.ownership_proof_sha256,
            }
        ),
        prefix="mig_sha256",
    )
    return PageUpdatePreflight(
        page_id=page_id,
        body_format="md",
        remote_title=remote_title,
        title=title,
        remote_version=remote_version,
        remote_storage=remote_storage,
        remote_storage_sha256=_sha256_text(remote_storage),
        candidate_storage=candidate.xhtml,
        candidate_storage_sha256=candidate_storage_sha256,
        migration_report=report_payload,
        migration_report_sha256=report_sha256,
        source_conversion_report=source_report,
        source_conversion_report_sha256=source_report_sha256,
        migration_fingerprint=fingerprint,
        consent_required=consent_required,
        ownership=ownership,
        external_image_count=external_image_count,
    )


def build_page_update_preflight(
    client: Any,
    page_id: str,
    body: str,
    *,
    body_format: str,
    title: str | None = None,
    if_version: int | None = None,
) -> PageUpdatePreflight:
    if body_format not in {"md", "storage"}:
        raise ValidationError("--body-format must be md or storage", context={"reason": "invalid_body_format"})
    page = client.get_page(page_id)
    remote_storage = getattr(page, "body_storage", None)
    if not isinstance(remote_storage, str):
        raise ValidationError("Confluence page storage body is missing", context={"reason": "storage_missing"})
    remote_version = _page_version(page)
    if if_version is not None and remote_version != if_version:
        raise StaleError(
            "Confluence page version changed",
            context={"reason": "page_version_mismatch", "expected": if_version, "actual": remote_version},
        )
    remote_title = getattr(page, "title", None)
    effective_title = title if title is not None else remote_title
    if not isinstance(remote_title, str):
        raise ValidationError("Confluence page title is missing", context={"reason": "title_missing"})
    if not isinstance(effective_title, str):
        raise ValidationError("Confluence page title is missing", context={"reason": "title_missing"})
    base_url = getattr(client, "base_url", None)
    if not isinstance(base_url, str):
        raise ValidationError("Confluence client base URL is missing", context={"reason": "site_missing"})
    if body_format == "md":
        return _build_markdown_update(
            page_id=page_id,
            site=site_fingerprint(base_url),
            remote_version=remote_version,
            remote_storage=remote_storage,
            markdown=body,
            remote_title=remote_title,
            title=effective_title,
        )
    try:
        cfxmark.to_md_artifact(body)
    except (cfxmark.CfxmarkError, TypeError, ValueError) as error:
        raise ValidationError(
            "Storage body is not valid Confluence storage",
            context={"reason": "storage_candidate_invalid", **conversion_failure_context(error)},
        ) from error
    empty_report_hash = _sha256_bytes(_canonical_json(_EMPTY_MIGRATION_REPORT))
    return PageUpdatePreflight(
        page_id=page_id,
        body_format="storage",
        remote_title=remote_title,
        title=effective_title,
        remote_version=remote_version,
        remote_storage=remote_storage,
        remote_storage_sha256=_sha256_text(remote_storage),
        candidate_storage=body,
        candidate_storage_sha256=_candidate_sha256(body),
        migration_report=dict(_EMPTY_MIGRATION_REPORT),
        migration_report_sha256=empty_report_hash,
        source_conversion_report={"schema": "atls-source-conversion-report-v1", "occurrences": []},
        source_conversion_report_sha256=_sha256_bytes(
            _canonical_json({"schema": "atls-source-conversion-report-v1", "occurrences": []})
        ),
        migration_fingerprint=None,
        consent_required=False,
        ownership=dict(_EMPTY_OWNERSHIP),
        external_image_count=0,
    )


def _consent_error(
    preflight: PageUpdatePreflight,
    *,
    argv: tuple[str, ...],
) -> MigrationConsentRequiredError:
    assert preflight.migration_fingerprint is not None
    return MigrationConsentRequiredError(
        "Markdown migration requires explicit informed consent",
        hint="Review the loss summary before running the returned command.",
        context={
            **to_error_context(preflight.to_dict()),
            "reason": "migration_consent_required",
            "accepted": False,
            "next_actions": [
                consent_retry_action(
                    argv,
                    option="--accept-migration",
                    fingerprint=preflight.migration_fingerprint,
                    description_code="REVIEW_MIGRATION_AND_RETRY",
                )
            ],
        },
    )


def publish_page_update(
    client: Any,
    preflight: PageUpdatePreflight,
    *,
    accept_migration: str | None,
    reason: str | None,
    minor_edit: bool,
    next_action_argv: tuple[str, ...],
) -> dict[str, Any]:
    # A proven no-op performs no remote mutation, so it must never demand migration
    # consent: decide no_change before the consent gate.
    if not preflight.would_update:
        return {
            **preflight.to_dict(),
            "status": "no_change",
            "put_count": 0,
            "version": preflight.remote_version,
        }
    if preflight.consent_required and accept_migration != preflight.migration_fingerprint:
        raise _consent_error(preflight, argv=next_action_argv)
    observed = client.get_page(preflight.page_id)
    observed_storage = getattr(observed, "body_storage", None)
    if (
        not isinstance(observed_storage, str)
        or _page_version(observed) != preflight.remote_version
        or _sha256_text(observed_storage) != preflight.remote_storage_sha256
        or getattr(observed, "title", None) != preflight.remote_title
    ):
        raise StaleError(
            "Confluence page changed immediately before update",
            context={"reason": "prewrite_remote_drift", "page_id": preflight.page_id},
        )
    version_reason = reason
    if preflight.body_format == "md":
        if preflight.migration_fingerprint is None:
            raise ValidationError(
                "Markdown update has no migration fingerprint for its version reason",
                context={"reason": "version_reason_fingerprint_missing"},
            )
        version_reason = proof_bound_version_reason(
            proof_mode="full_migration",
            fingerprint=preflight.migration_fingerprint,
            migration_report=preflight.migration_report,
            user_reason=reason,
        )
    update_error: Exception | None = None
    try:
        client.update_page(
            preflight.page_id,
            preflight.title,
            preflight.candidate_storage,
            preflight.remote_version + 1,
            body_format="storage",
            reason=version_reason,
            minor_edit=minor_edit,
        )
    except Exception as error:  # server may commit before the response is lost
        update_error = error
    try:
        readback = client.get_page(preflight.page_id)
    except Exception as error:
        raise ConflictError(
            "Page update outcome is unknown because read-back failed",
            context={"reason": "page_update_readback_failed", "page_id": preflight.page_id},
        ) from (update_error or error)
    readback_storage = getattr(readback, "body_storage", None)
    # Confluence re-serializes storage on save (text quotes become &quot; entities,
    # void elements gain a space before the slash) and stamps a server-assigned
    # ac:macro-id on every macro, so the byte hash mismatches even for an unchanged
    # save. Fall back to the read-back comparator, which ignores those
    # serialization-only diffs and the server macro-id while still rejecting any
    # real content, structure, parameter, or other attribute change; its value-free
    # public summary explains any rejection. The strict semantic comparator remains
    # the basis of the ownership proof.
    readback_comparison = (
        cfxmark.storage_readback_comparison(preflight.candidate_storage, readback_storage)
        if isinstance(readback_storage, str)
        else None
    )
    candidate_observed = (
        isinstance(readback_storage, str)
        and _page_version(readback) == preflight.remote_version + 1
        and getattr(readback, "title", None) == preflight.title
        and (
            _sha256_text(readback_storage) == preflight.candidate_storage_sha256
            or (readback_comparison is not None and readback_comparison.equivalent)
        )
    )
    if candidate_observed:
        return {
            **preflight.to_dict(),
            "status": "updated",
            "version": preflight.remote_version + 1,
            "put_count": 1,
            **({"recovery": "lost_response_adopted"} if update_error is not None else {}),
        }
    # Server accepted the PUT but left the version unchanged because the candidate is
    # a semantic no-op against the stored body (it differs only by the server macro
    # envelope). This is a distinct, truthful outcome from a skipped PUT: one PUT was
    # made. Only when the response came back cleanly; the NOOP-family comparator is
    # correct here because the read-back carries the server-assigned ids the
    # candidate lacks. A real read-back mutation is not equivalent and falls through.
    if (
        update_error is None
        and isinstance(readback_storage, str)
        and _page_version(readback) == preflight.remote_version
        and getattr(readback, "title", None) == preflight.title
        and cfxmark.storage_noop_comparison(preflight.candidate_storage, readback_storage).equivalent
    ):
        return {
            **preflight.to_dict(),
            "status": "reconciled_no_change",
            "version": preflight.remote_version,
            "put_count": 1,
            "recovery": "server_noop_no_version_bump",
        }
    source_observed = (
        isinstance(readback_storage, str)
        and _page_version(readback) == preflight.remote_version
        and _sha256_text(readback_storage) == preflight.remote_storage_sha256
    )
    if update_error is not None and source_observed:
        raise ValidationError(
            "Page update did not change the remote page",
            context={"reason": "page_update_put_failed", "page_id": preflight.page_id},
        ) from update_error
    if update_error is not None:
        raise ConflictError(
            "Page update outcome is ambiguous",
            context={"reason": "page_update_outcome_ambiguous", "page_id": preflight.page_id},
        ) from update_error
    mismatch_summary = storage_comparison_context(readback_comparison)
    raise ValidationError(
        "Page update read-back did not match the source-bound candidate",
        context={
            "reason": "page_update_readback_mismatch",
            "page_id": preflight.page_id,
            **({"comparison": mismatch_summary} if mismatch_summary is not None else {}),
        },
    )


def _page_matches_create(
    page: Any,
    *,
    space: str,
    title: str,
    parent_id: str | None,
    candidate_storage: str,
) -> bool:
    page_space = getattr(getattr(page, "space", None), "key", None)
    ancestors = getattr(page, "ancestors", ())
    observed_parent = str(ancestors[-1].id) if ancestors else None
    storage = getattr(page, "body_storage", None)
    # Title, space, parent, and status stay strict identity checks; only the
    # stored body tolerates the server's save-time re-serialization and macro-id
    # stamping, via the same read-back comparator the update path uses.
    return (
        getattr(page, "status", None) in (None, "current")
        and getattr(page, "title", None) == title
        and page_space == space
        and observed_parent == parent_id
        and isinstance(storage, str)
        and (
            _sha256_text(storage) == _sha256_text(candidate_storage)
            or cfxmark.storage_readback_comparison(candidate_storage, storage).equivalent
        )
    )


def _reconcile_created_page(
    client: Any,
    *,
    space: str,
    title: str,
    parent_id: str | None,
    candidate_storage: str,
) -> Any | None:
    ids = _title_candidate_ids(client, space=space, title=title)
    if len(ids) != 1:
        return None
    page = client.get_page(ids[0], expand="body.storage,version,space,ancestors")
    if not _page_matches_create(
        page,
        space=space,
        title=title,
        parent_id=parent_id,
        candidate_storage=candidate_storage,
    ):
        return None
    return page


def _assert_create_target_available(client: Any, *, space: str, title: str) -> None:
    candidates = _title_candidate_ids(client, space=space, title=title)
    if candidates:
        raise ConflictError(
            "Destination space already contains this page title",
            context={"reason": "page_create_title_exists", "candidate_ids": list(candidates)},
        )


def create_page_stateless(
    client: Any,
    *,
    space: str,
    title: str,
    parent_id: str | None,
    body: str,
    body_format: str,
    dry_run: bool,
    accept_conversion: str | None,
    next_action_argv: tuple[str, ...],
) -> dict[str, Any]:
    if body_format not in {"md", "storage"}:
        raise ValidationError("--body-format must be md or storage", context={"reason": "invalid_body_format"})
    consent_action: dict[str, Any] | None = None
    if body_format == "md":
        conversion = build_source_conversion(body)
        candidate = conversion.candidate_storage
        proof = conversion.to_dict()
        if conversion.consent_required:
            assert conversion.conversion_fingerprint is not None
            consent_action = consent_retry_action(
                next_action_argv,
                option="--accept-conversion",
                fingerprint=conversion.conversion_fingerprint,
                description_code="REVIEW_CONVERSION_AND_RETRY",
            )
        if consent_action is not None and not dry_run and accept_conversion != conversion.conversion_fingerprint:
            raise ConversionConsentRequiredError(
                "Markdown source conversion requires explicit informed consent",
                hint="Review the loss summary before running the returned command.",
                context={
                    **to_error_context(proof),
                    "reason": "conversion_consent_required",
                    "accepted": False,
                    "next_actions": [consent_action],
                },
            )
    else:
        try:
            cfxmark.to_md_artifact(body)
        except (cfxmark.CfxmarkError, TypeError, ValueError) as error:
            raise ValidationError(
                "Storage body is not valid Confluence storage",
                context={"reason": "storage_candidate_invalid", **conversion_failure_context(error)},
            ) from error
        candidate = body
        proof = {
            "candidate_storage_sha256": _sha256_text(candidate),
            "consent_required": False,
            "asset_sync": False,
        }
    if dry_run:
        return {
            **proof,
            "status": "conversion_consent_required" if proof.get("consent_required") else "ready_to_create",
            "would_create": True,
            "space": space,
            "title": title,
            "parent_id": parent_id,
            "method": "POST",
            "post_count": 0,
            **({"next_actions": [consent_action]} if consent_action is not None else {}),
        }
    _assert_create_target_available(client, space=space, title=title)
    create_error: Exception | None = None
    response: Any = None
    try:
        response = client.create_page(space, title, candidate, ancestor_id=parent_id, body_format="storage")
    except Exception as error:  # server may commit before the response is lost
        create_error = error
    page = None
    if create_error is None and isinstance(response, dict) and response.get("id") is not None:
        try:
            candidate_page = client.get_page(str(response["id"]), expand="body.storage,version,space,ancestors")
        except Exception as error:
            raise ConflictError(
                "Page create outcome is unknown because read-back failed",
                context={"reason": "page_create_readback_failed"},
            ) from error
        if _page_matches_create(
            candidate_page,
            space=space,
            title=title,
            parent_id=parent_id,
            candidate_storage=candidate,
        ):
            page = candidate_page
        else:
            observed_storage = getattr(candidate_page, "body_storage", None)
            create_mismatch = storage_comparison_context(
                cfxmark.storage_readback_comparison(candidate, observed_storage)
                if isinstance(observed_storage, str)
                else None
            )
            raise ValidationError(
                "Created page read-back did not match the source-bound candidate",
                context={
                    "reason": "page_create_readback_mismatch",
                    "page_id": str(response["id"]),
                    **({"comparison": create_mismatch} if create_mismatch is not None else {}),
                },
            )
    else:
        try:
            page = _reconcile_created_page(
                client,
                space=space,
                title=title,
                parent_id=parent_id,
                candidate_storage=candidate,
            )
        except Exception as error:
            raise ConflictError(
                "Page create outcome is unknown and reconciliation failed",
                context={"reason": "page_create_reconciliation_failed"},
            ) from (create_error or error)
        if page is None:
            raise ConflictError(
                "Page create outcome is ambiguous; no duplicate POST was attempted",
                context={"reason": "page_create_outcome_ambiguous"},
            ) from create_error
    return {
        **proof,
        "status": "created",
        "id": str(page.id),
        "title": title,
        "space": space,
        "parent_id": parent_id,
        "version": _page_version(page),
        "post_count": 1,
        **({"recovery": "lost_response_adopted"} if create_error is not None else {}),
    }
