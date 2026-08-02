"""Authoritative state-free managed Markdown publication preflight."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, replace
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

import cfxmark

from atlassian_skills.confluence.asset_sync import (
    AssetAction,
    AssetBaseline,
    bind_managed_attachment_markdown,
    build_asset_plan,
    extract_managed_asset_references,
    rewrite_attachment_artifact,
    snapshot_remote_attachments,
)
from atlassian_skills.confluence.compatibility import (
    candidate_loss,
    canonicalization_sites,
    compatibility_digest,
    compatibility_payload,
)
from atlassian_skills.confluence.managed_operation import (
    ManagedAssetOperation,
    ManagedOperation,
    attachment_inventory_sha256,
)
from atlassian_skills.confluence.preservation import ragged_protected_table_paths
from atlassian_skills.confluence.proof_mootness import Mootness, assess_proof_mootness
from atlassian_skills.confluence.sidecar import read_authority
from atlassian_skills.core.errors import StaleError, ValidationError
from atlassian_skills.core.file_identity import inspect_file_identity
from atlassian_skills.core.managed_file import read_managed_utf8, resolve_managed_asset_path
from atlassian_skills.core.managed_manifest import (
    ManagedAssetRecord,
    ManagedDocument,
    ManagedManifestError,
    canonical_asset_set_sha256,
    canonical_content_sha256,
    canonical_managed_content,
    extract_asset_records,
    parse_managed_document,
    parse_passthrough,
    serialize_passthrough,
)
from atlassian_skills.core.site_identity import site_fingerprint

_CANONICAL_EQUIVALENT_CODES: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ManagedAssetPlanItem:
    src: str
    remote_id: str | None
    remote_version: int | None
    remote_name: str
    baseline_sha256: str | None
    current_sha256: str
    materialization: str
    action: str
    pre_upload_ids: str | None
    local_path: Path = dataclass_field(repr=False, compare=False)
    local_file_identity: str | None = dataclass_field(default=None, repr=False, compare=False)
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.pop("local_path")
        result.pop("local_file_identity")
        return result

    def proof_dict(self) -> dict[str, Any]:
        result = self.to_dict()
        result.pop("reason")
        return result


@dataclass(frozen=True)
class ManagedPreflight:
    proof_mode: str
    status: str
    page_id: str
    site: str
    managed_file_identity: str
    managed_file_sha256: str
    managed_content_sha256: str
    managed_assets_sha256: str
    remote_version: int
    remote_storage_sha256: str
    body_dirty: bool
    asset_dirty: bool
    candidate_storage_sha256: str
    migration_report: dict[str, Any]
    migration_report_sha256: str
    migration_fingerprint: str | None
    append_sha256: str | None
    append_fragment_sha256: str | None
    consent_required: bool
    asset_plan_sha256: str
    asset_plan: tuple[ManagedAssetPlanItem, ...]
    ownership: dict[str, Any]
    deferred_migrations: tuple[dict[str, Any], ...]
    candidate_storage: str
    source_storage: str
    base_markdown: str
    edited_markdown: str
    document: ManagedDocument
    candidate: cfxmark.CfxArtifact | None
    #: The file this preflight is about, carried so the next steps it returns are
    #: commands rather than commands with a hole where the path goes.
    managed_path: str | None = None
    #: Fresh answers computed inside this preflight. These are not pull-time
    #: forecasts: they are bound to `source_storage`, the same remote bytes the
    #: candidate and proof used.
    compatibility: dict[str, Any] | None = None
    candidate_loss_payload: dict[str, Any] | None = None

    @property
    def would_update(self) -> bool:
        return self.body_dirty or self.asset_dirty

    @property
    def presentation_occurrences(self) -> int:
        """How many places this publish hands the platform its own canonical form.

        Read from the same `candidate_loss` the dry-run reports, so the number on
        the receipt is the number the caller was shown before agreeing to anything.
        Recomputing it separately for the receipt is how the two drift.
        """

        loss = self.candidate_loss_payload or candidate_loss(self.source_storage, self.candidate_storage)
        return int(loss["affected_occurrences"])

    def to_dict(self) -> dict[str, Any]:
        compatibility = self.compatibility or compatibility_payload(
            self.page_id,
            self.source_storage,
            document_path=self.managed_path,
        )
        loss = self.candidate_loss_payload or candidate_loss(self.source_storage, self.candidate_storage)
        return {
            "status": self.status,
            "proof_mode": self.proof_mode,
            "page_id": self.page_id,
            "remote_version": self.remote_version,
            "body": {"dirty": self.body_dirty, "would_update": self.body_dirty},
            "assets": {
                "dirty": self.asset_dirty,
                "upload": sum(item.action in {"create", "update"} for item in self.asset_plan),
                "unchanged": sum(item.action == "unchanged" for item in self.asset_plan),
                "delete": 0,
                "items": [item.to_dict() for item in self.asset_plan],
            },
            "would_update": self.would_update,
            "candidate_storage_sha256": self.candidate_storage_sha256,
            "migration_report": self.migration_report,
            "migration_report_sha256": self.migration_report_sha256,
            "migration_fingerprint": self.migration_fingerprint,
            "append_sha256": self.append_sha256,
            "append_fragment_sha256": self.append_fragment_sha256,
            "consent_required": self.consent_required,
            "asset_plan_sha256": self.asset_plan_sha256,
            "ownership": self.ownership,
            "deferred_migrations": list(self.deferred_migrations),
            # Computed against the fresh remote body fetched by this preflight,
            # never carried from pull time. Reusing it while serialising this
            # immutable result avoids asking the same expensive question twice.
            "compatibility": compatibility,
            # The forecast above says what this page would cost to regenerate from
            # scratch. This says what the document about to be written actually
            # drops, which is the number an approval should be bound to.
            "candidate_loss": loss,
            # The publish decision, named apart from the workflow one and lifted
            # where it cannot be missed. `compatibility.workflow_decision_required`
            # asks which representation should manage this page; this asks whether
            # *this candidate* needs the author to agree to something. They were
            # one field called `requires_user_approval` in two payloads read
            # minutes apart.
            #
            # The gate's own value, not a second expression computed from
            # `candidate_loss`. That recomputation counted named losses and
            # presentation changes and silently missed the third trigger, a migration
            # occurrence -- so an emoticon page reported `false` here while
            # `consent_required` held `true` and the push refused. SKILL.md tells an
            # agent to branch on this field and no other, so the two disagreeing is a
            # public contract contradicting itself. Anything that decides consent has
            # to decide it once.
            "publish_consent_required": self.consent_required,
        }


def _canonical_json(value: Any) -> bytes:
    def normalize(item: Any) -> Any:
        if isinstance(item, str):
            return unicodedata.normalize("NFC", item)
        if isinstance(item, dict):
            return {key: normalize(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(child) for child in item]
        return item

    return json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes, *, prefix: str = "sha256") -> str:
    return f"{prefix}:{hashlib.sha256(value).hexdigest()}"


def _sha256_text(value: str, *, prefix: str = "sha256") -> str:
    return _sha256_bytes(value.encode("utf-8"), prefix=prefix)


def _remote_storage_hash(storage: str) -> str:
    return _sha256_text(storage)


def _page_version(page: Any) -> int:
    version = getattr(page, "version", None)
    return int(getattr(version, "number", version) or 1)


def _migration_occurrence_payload(
    occurrence: Any, *, display: bool, consent_required: bool = True, change_kind: str = "content"
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "occurrence_id": occurrence.occurrence_id,
        "code": occurrence.code,
        "effect": str(occurrence.effect),
        "category": occurrence.category,
        # Marked rather than removed. The report is the factual record of what the
        # publish changes, and dropping an entry from it to avoid a prompt would
        # hide a change from the person the record exists for. Marking says both
        # things at once: this node changes, and you are not being asked about it.
        "consent_required": consent_required,
        # `presentation` or `content`. Both need approval; they do not need the same sentence.
        "change_kind": change_kind,
        "location_identity": [
            {key: value for key, value in asdict(component).items() if value is not None}
            for component in occurrence.location_identity
        ],
    }
    for field in ("before_fingerprint", "after_fingerprint"):
        value = getattr(occurrence, field)
        if value is not None:
            payload[field] = value
    if display:
        for field in (
            "before_summary",
            "after_summary",
            "user_impact",
            "suggested_workflow",
            "display_label",
        ):
            value = getattr(occurrence, field)
            if value is not None:
                payload[field] = value
    return payload


def _migration_report_payload(
    report: Any,
    *,
    display: bool,
    occurrence_ids: frozenset[str] | None = None,
    canonicalized_sites: frozenset[tuple[str, ...]] = frozenset(),
) -> dict[str, Any]:
    occurrences = [
        _migration_occurrence_payload(
            item,
            display=display,
            # Every reported occurrence needs consent now. What the classification still buys
            # is `change_kind`: a caller can tell "your readers will see different spacing"
            # from "a sentence is going away" and say so when asking.
            consent_required=True,
            change_kind=("presentation" if _is_canonicalization(item, canonicalized_sites) else "content"),
        )
        for item in report.occurrences
        if occurrence_ids is None or item.occurrence_id in occurrence_ids
    ]
    occurrences.sort(
        key=lambda item: (
            _canonical_json(item["location_identity"]),
            item["code"],
            item["occurrence_id"],
        )
    )
    return {"schema": report.schema, "occurrences": occurrences}


def _report_hash(
    report: Any,
    *,
    occurrence_ids: frozenset[str] | None = None,
    canonicalized_sites: frozenset[tuple[str, ...]] = frozenset(),
) -> str:
    # The marker is inside the hash on purpose. Consent is bound to the report the
    # caller was shown, and "you were not asked about this one" is part of what they
    # were shown.
    return _sha256_bytes(
        _canonical_json(
            _migration_report_payload(
                report,
                display=False,
                occurrence_ids=occurrence_ids,
                canonicalized_sites=canonicalized_sites,
            )
        )
    )


#: Report codes whose occurrence is not a loss when it lands on a node cfxmark's
#: registry classifies as `platform_editor_canonicalization`.
#:
#: Two conditions, not one. The code alone is too coarse -- the same
#: `empty-paragraph-dropped` is a real loss in a list item, and it is 18 of the 19
#: occurrences on the 24 adoptable pages of the live corpus, so excusing the code outright would
#: quietly stop asking about almost every one of them. The path alone is too coarse
#: the other way: a different code arriving at the same node later would inherit an
#: excuse that was never about it.
_CANONICALIZED_CODES: frozenset[str] = frozenset({"empty-paragraph-dropped"})


def _occurrence_path(occurrence: Any) -> tuple[str, ...]:
    return tuple(
        str(part.name)
        for part in getattr(occurrence, "location_identity", ())
        if str(getattr(part, "kind", "")) == "path"
    )


def _consent_required(
    report: Any,
    *,
    occurrence_ids: frozenset[str] | None = None,
    canonicalized_sites: frozenset[tuple[str, ...]] = frozenset(),
) -> bool:
    for occurrence in report.occurrences:
        if occurrence_ids is not None and occurrence.occurrence_id not in occurrence_ids:
            continue
        effect = str(occurrence.effect)
        if effect in {"converted", "removed", "unsupported"}:
            return True
        if effect == "normalized" and occurrence.code not in _CANONICAL_EQUIVALENT_CODES:
            return True
    return False


def _is_canonicalization(occurrence: Any, canonicalized_sites: frozenset[tuple[str, ...]]) -> bool:
    """Whether this reported change is the platform's own canonical form arriving.

    **This no longer waives consent, and that is a reversal.** It did, on the grounds that
    Confluence's editor replaces `<p/>` on a no-edit save, so the form is not one an author
    can keep. R4-pre rejected the inference and was right: a browser save converging is not
    evidence that *our* REST publish may converge it, and a REST no-op measurably does not.
    Measured on the real paths -- the append proof splices the untouched bytes and changes
    nothing, while `full_migration` rewrites every untouched `<p/>` on the page from an edit
    made somewhere else. That is us changing what the author's readers see.

    So the classification is kept and the waiver is not. It still tells a caller this is a
    presentation change rather than a content loss, which is what `consent_required: false`
    used to say by omission and now says by name.

    The shape is cfxmark's to define and is asked for rather than reproduced here:
    `canonicalization_sites` reads the stored page through the same scope predicate
    the compatibility verdict uses. A second implementation of "between ordinary
    body blocks" living in this file is how the two stop agreeing, and the direction
    it would drift is towards excusing more.
    """

    return occurrence.code in _CANONICALIZED_CODES and _occurrence_path(occurrence) in canonicalized_sites


def _asset_plan(
    client: Any,
    page_id: str,
    managed_path: Path,
    markdown: str,
    records: tuple[ManagedAssetRecord, ...],
) -> tuple[tuple[ManagedAssetPlanItem, ...], str, bool]:
    try:
        references = extract_managed_asset_references(markdown)
    except ValueError as error:
        raise ValidationError(
            "Managed asset reference is not representable",
            context={"reason": "managed_asset_reference_invalid", "classification": "unrepresentable_reference"},
        ) from error
    baselines = tuple(
        AssetBaseline(
            attachment_id=record.remote_id,
            attachment_version=record.remote_version,
            filename=record.remote_name,
            local_path=resolve_managed_asset_path(managed_path, record.src),
            content_sha256=record.sha256.removeprefix("sha256:"),
            media_type=None,
            reference_state="referenced_local" if record.materialization == "local" else "remote_only",
        )
        for record in records
    )
    remote = snapshot_remote_attachments(client, page_id) if references or baselines else ()
    pre_upload_ids = attachment_inventory_sha256(item.attachment_id for item in remote)
    plan = build_asset_plan(
        managed_path=managed_path,
        references=(reference.local_reference for reference in references),
        baselines=baselines,
        remote=remote,
        reference_remote_names={reference.local_reference: reference.remote_filename for reference in references},
    )
    conflicts = plan.conflicts
    if conflicts:
        item = conflicts[0]
        baseline_drift = item.reason in {
            "remote_attachment_missing",
            "remote_attachment_drift",
        }
        error_type = StaleError if baseline_drift else ValidationError
        raise error_type(
            "Managed attachment plan is not publishable",
            context={
                "reason": "remote_asset_stale" if baseline_drift else "managed_asset_conflict",
                "asset_reason": item.reason,
                "src": item.reference,
                "remote_id": item.baseline.attachment_id if item.baseline is not None else None,
            },
        )

    action_names = {
        AssetAction.UNCHANGED: "unchanged",
        AssetAction.NEW: "create",
        AssetAction.NEW_VERSION: "update",
        AssetAction.UNREFERENCED: "unreferenced",
    }
    items = []
    for item in plan.items:
        if item.action is AssetAction.CONFLICT:
            raise AssertionError("asset conflicts must be rejected before materialization")
        baseline = item.baseline
        observed = item.remote
        materialization = (
            "remote-only" if baseline is not None and baseline.reference_state == "remote_only" else "local"
        )
        current_sha = item.content_sha256 or (baseline.content_sha256 if baseline is not None else None)
        if current_sha is None:
            raise ValidationError(
                "Managed attachment plan has no content identity",
                context={"reason": "managed_asset_hash_missing", "src": item.reference},
            )
        items.append(
            ManagedAssetPlanItem(
                src=item.reference,
                remote_id=(
                    baseline.attachment_id if baseline is not None else observed.attachment_id if observed else None
                ),
                remote_version=(
                    baseline.attachment_version if baseline is not None else observed.version if observed else None
                ),
                remote_name=item.remote_filename,
                baseline_sha256=(
                    f"sha256:{baseline.content_sha256}"
                    if baseline is not None
                    else f"sha256:{observed.content_sha256}"
                    if observed is not None and item.action is AssetAction.UNCHANGED
                    else None
                ),
                current_sha256=f"sha256:{current_sha}",
                materialization=materialization,
                action=action_names[item.action],
                pre_upload_ids=(pre_upload_ids if item.action is AssetAction.NEW else None),
                local_path=item.local_path,
                local_file_identity=item.local_file_identity,
                reason=item.reason,
            )
        )
    items.sort(key=lambda item: (item.src.encode("utf-8"), item.remote_name.encode("utf-8")))
    digest = _sha256_bytes(_canonical_json([item.proof_dict() for item in items if item.action != "unreferenced"]))
    return tuple(items), digest, any(item.action in {"create", "update"} for item in items)


def rebuild_managed_asset_plan(
    managed_path: Path,
    assets: tuple[ManagedAssetOperation, ...],
) -> tuple[tuple[ManagedAssetPlanItem, ...], str, bool]:
    items: list[ManagedAssetPlanItem] = []
    for asset in assets:
        local_path = resolve_managed_asset_path(managed_path, asset.src)
        local_file_identity: str | None = None
        if asset.materialization == "local":
            try:
                local_file_identity = inspect_file_identity(local_path).key
                observed_sha256 = _sha256_bytes(local_path.read_bytes())
            except (OSError, ValidationError) as error:
                raise ValidationError(
                    "Managed local asset is missing or unsafe during recovery",
                    context={"reason": "manual_recovery_local_changed", "src": asset.src},
                ) from error
            if observed_sha256 != asset.local_sha256:
                raise ValidationError(
                    "Managed local asset changed during recovery",
                    context={"reason": "manual_recovery_local_changed", "src": asset.src},
                )
        items.append(
            ManagedAssetPlanItem(
                src=asset.src,
                remote_id=asset.baseline_id,
                remote_version=asset.baseline_version,
                remote_name=asset.remote_name,
                baseline_sha256=asset.baseline_sha256,
                current_sha256=asset.local_sha256,
                materialization=asset.materialization,
                action=asset.action,
                pre_upload_ids=asset.pre_upload_ids,
                local_path=local_path,
                local_file_identity=local_file_identity,
            )
        )
    digest = _sha256_bytes(_canonical_json([item.proof_dict() for item in items]))
    return tuple(items), digest, any(item.action in {"create", "update"} for item in items)


def _ownership_payload(candidate: cfxmark.CfxArtifact) -> dict[str, Any]:
    proof = candidate.ownership_proof
    if proof is None:
        return {
            "intended_operation_ids": [],
            "accepted_migration_occurrence_ids": [],
            "unclassified": [],
            "multiple_owners": [],
            "overlap": [],
            "fatal_diagnostic_codes": [],
        }
    return {
        "intended_operation_ids": sorted({item.owner for item in proof.intended}),
        "accepted_migration_occurrence_ids": sorted({item.owner for item in proof.migration}),
        "unclassified": [asdict(item) for item in proof.unclassified],
        "multiple_owners": [asdict(item) for item in proof.multiple_owners],
        "overlap": [asdict(item) for item in proof.overlapping],
        "fatal_diagnostic_codes": list(proof.fatal_diagnostic_codes),
        "proof_sha256": candidate.ownership_proof_sha256,
        "final_change_count": candidate.ownership_final_change_count,
    }


# ---------------------------------------------------------------------------
# G6 value-free error/consent projections.
#
# Every managed-Confluence JSON error or consent envelope must be value-free: no
# storage XHTML, leaf value/fingerprint, credential, attachment filename, raw
# user source, or arbitrary exception string. Only canonical identity (path /
# field / attribute), counts, diagnostic/resolution codes, and policy identifiers
# cross the boundary. These adapters build that projection from the cfxmark public
# diagnostic types (OwnershipProofSummary, StorageComparison) and from the display
# report payloads reused in error/consent contexts. Normal (non-error) report
# rendering keeps its richer display payload and is deliberately not touched here.
# ---------------------------------------------------------------------------

_CONVERSION_FAILURE_CODES = {
    "ParseError": "parse_error",
    "ConversionError": "conversion_error",
    "OwnershipProofError": "ownership_proof_error",
    "MacroError": "macro_error",
    "AssetSecurityError": "asset_security_error",
    "TypeError": "invalid_input",
    "ValueError": "invalid_input",
}

# Occurrence keys safe to serialize in an error/consent envelope: value-free
# identity, classification, and resolution only. Message text, before/after
# summaries, fingerprints, and asset names are dropped by omission (deny by
# default), so a new unsafe cfxmark field can never silently leak.
_SAFE_OCCURRENCE_KEYS = frozenset(
    {
        "occurrence_id",
        "code",
        "effect",
        "category",
        "severity",
        "location",
        "location_identity",
        "resolutions",
        # Two fields carrying no page content, and the ones a consent envelope is least able
        # to do without: what is being asked about, and whether it is the author's words or
        # their readers' spacing.
        "consent_required",
        "change_kind",
    }
)

# Asset-plan item keys safe to serialize in an error/consent envelope. Deny by
# default: the portable ``src`` path and the server ``remote_name`` both carry the
# attachment filename and are dropped by omission; only counts, action, content
# hashes, opaque ids, and the value-free reason code survive.
_SAFE_ASSET_ITEM_KEYS = frozenset(
    {
        "remote_id",
        "remote_version",
        "baseline_sha256",
        "current_sha256",
        "materialization",
        "action",
        "pre_upload_ids",
        "reason",
    }
)


def conversion_failure_code(error: BaseException) -> str:
    """Return a stable, value-free code classifying a conversion failure by type."""

    return _CONVERSION_FAILURE_CODES.get(type(error).__name__, "conversion_error")


def conversion_failure_context(error: BaseException) -> dict[str, str]:
    """Return the value-free error context that classifies a conversion failure.

    Always carries the generic-by-type ``conversion_code`` (unchanged, so every
    existing envelope stays backwards compatible). Additionally carries a specific
    ``conversion_reason_code`` iff the error exposes a structured ``reason_code``
    attribute whose value is in cfxmark's published allowlist
    (:data:`cfxmark.PUBLIC_CONVERSION_REASON_CODES`). The exception message string
    is never parsed, and a code outside the allowlist is dropped (fallback to the
    generic code), so only a closed-enum machine identifier can ever cross the
    boundary — never a message, page content, leaf text, or node value.
    """

    context: dict[str, str] = {"conversion_code": conversion_failure_code(error)}
    reason_code = getattr(error, "reason_code", None)
    if isinstance(reason_code, str) and reason_code in cfxmark.PUBLIC_CONVERSION_REASON_CODES:
        context["conversion_reason_code"] = reason_code
        # The code alone is not actionable, and this branch used to stop here.
        # Measured on a real document: a live publish refused with
        # `ownership_proof_invalid/semantic-mapping-ambiguous` and its caller had
        # nothing to print but those two words -- the sentence explaining what the
        # proof could not decide, and the two edit shapes that avoid it, existed
        # in this module and never reached the envelope.
        #
        # Static atls-authored text keyed by a stable code, exactly as the summary
        # branch does. Never cfxmark's message or display_label, which can carry
        # page content.
        description = describe_migration_code(reason_code)
        if description:
            context["conversion_reason_description"] = description
        context["supported_alternatives"] = list(SUPPORTED_ALTERNATIVES)  # type: ignore[assignment]
    return context


# Value-free, atls-authored one-line descriptions for cfxmark's STABLE migration /
# diagnostic codes, used only to make the human consent console line readable. These
# are static constants — NEVER derived from cfxmark's message / display_label (which
# can carry arbitrary content) — so they are safe to show and would be safe to
# serialize. The JSON envelope is deliberately left minimal (code only); the curated
# text is a human-console enrichment. An unmapped code falls back to the raw code at
# the call site. Keyed by the consent-reachable codes in cfxmark's producer registry
# (converted / removed / normalized / unsupported / preserved) plus the one fatal
# code an operator must act on.
MIGRATION_CODE_DESCRIPTIONS: dict[str, str] = {
    "emoticon-to-unicode": "Confluence emoticon converted to a Unicode character",
    "attachment-url-components-unrenderable": "Attachment URL query/fragment components cannot be represented in Markdown",
    "body-only-table-header-synthesized": "A header row was synthesized for a table that had only a body",
    "html-tag-stripped": "An inline HTML tag was stripped",
    "layout-flattened": "A multi-column layout was flattened to a single column",
    "table-cell-background-omitted": "A table cell background colour was omitted from the Markdown body",
    "table-cell-background-unrenderable": "A table cell background colour cannot be represented in Markdown",
    "table-cell-background-untracked": "A table cell background colour could not be tracked and was dropped",
    "table-column-alignment-mixed": "A table column with mixed cell alignment was normalized to a single alignment",
    "xml-comment-dropped": "An XML comment was dropped",
    "html-comment-dropped": "An HTML comment was dropped",
    "empty-paragraph-dropped": "An empty paragraph (vertical spacing only) was dropped",
    "table-topology-changed": "The table's shape changed, so its stored cell presentation was dropped",
    "list-item-paragraph-unwrapped": "A paragraph inside a list item was unwrapped to plain list-item text",
    "trailing-space-trimmed": "Trailing whitespace was trimmed",
    "unsupported-confluence-content": "Confluence content that cannot be represented in Markdown",
    "nested-ordered-list-start-unrepresentable": (
        "A nested numbered list starting at a non-1 number cannot be represented in Markdown; "
        "rewrite it as a flat list in Confluence and re-pull"
    ),
    # Fatal proof classes. These are the codes an in-place edit actually dies on, so
    # they need to say *which* question the proof could not answer -- "the proof did
    # not hold" alone sends an operator hunting through the document. Each names the
    # ambiguity and the edit shape that avoids it.
    "table-presentation-ambiguous": (
        "A table's stored cell presentation could not be matched to exactly one table in the "
        "edited Markdown; publishing could move that presentation onto the wrong table"
    ),
    "unclassified-storage-change": (
        "A change in the stored page could not be attributed to any edit in the Markdown, so the "
        "publish cannot prove which stored content it would replace"
    ),
    "multiple-change-owners": (
        "A change in the stored page could be explained by more than one Markdown edit; the "
        "publish cannot tell which one it belongs to"
    ),
    "semantic-mapping-ambiguous": (
        "The edit has more than one equally likely reading (for example delete-then-insert versus "
        "move); adding blocks at the end of the document instead avoids the ambiguity"
    ),
    "semantic-source-map-incomplete": (
        "Part of the stored page could not be mapped to the Markdown, so an in-place edit cannot "
        "be proven; append-only edits and single-leaf patches still work"
    ),
}

#: The two edit shapes that do not need a source-bound ownership proof. Named
#: once because it is now answered on three refusal paths, and a list that says
#: what a caller can still do is worth being the same list every time.
SUPPORTED_ALTERNATIVES = ["append_markdown_blocks", "page_patch_text", "full_replacement_with_consent"]

# Value-free next step for a failed in-place proof. The JSON envelope already carries
# `fatal_class` and `supported_alternatives`, but plain output prints only the message
# (cli/main.py `_emit_entrypoint_error`), so without this an operator sees one sentence
# with no way to narrow it. Static text -- no page, code, or leaf value crosses here.
OWNERSHIP_PROOF_HINT = (
    "Re-run with --format=json for error.context.fatal_class and supported_alternatives. "
    "Appending blocks at the end of the document, page patch-text for one exact string, or an explicit "
    "full replacement with the returned approval fingerprint does not use this proof."
)

# A managed file records the converter that produced it; a cfxmark upgrade therefore
# invalidates every managed file until it is re-pulled. Without this the operator gets
# "not current" and no way to act on it.
MANAGED_CONVERTER_HINT = "Re-pull the page with `page pull-md` to adopt the current converter."


def describe_migration_code(code: str | None) -> str | None:
    """Return the value-free curated human description for a stable code, or None if unmapped."""

    if not isinstance(code, str):
        return None
    return MIGRATION_CODE_DESCRIPTIONS.get(code)


def _leaf_identity_context(identity: Any) -> dict[str, Any]:
    """Project a cfxmark leaf identity tuple ``(path, field, attribute, ordinal)`` to value-free JSON."""

    path, field, attribute_name, ordinal = identity
    context: dict[str, Any] = {"path": [str(component) for component in path], "field": field, "ordinal": ordinal}
    if attribute_name is not None:
        context["attribute"] = attribute_name
    return context


def ownership_error_context(source: Any, *, reason: str) -> dict[str, Any]:
    """Adapt a cfxmark ``OwnershipProofSummary`` or an ownership payload dict to a value-free context.

    The summary path (fatal publish-preflight boundary) reads cfxmark's value-free
    projection directly. The payload path (defensive fatal-group check) reduces the
    ``asdict`` leaf dumps to their canonical identities and counts, dropping the
    per-leaf fingerprints those dumps carry.
    """

    if isinstance(source, cfxmark.OwnershipProofSummary):
        return {
            "reason": reason,
            "fatal_class": source.fatal_class,
            # The curated description turns a bare code into the question the proof
            # could not answer. Static atls-authored text keyed by a stable code —
            # never cfxmark's message/display_label, which can carry page content.
            "fatal_class_description": describe_migration_code(source.fatal_class),
            "counts": {
                "unclassified": source.unclassified_count,
                "multiple_owners": source.multiple_owners_count,
                "overlap": source.overlap_count,
            },
            "identities": [_leaf_identity_context(identity) for identity in source.top_identities],
            # The same value-free shapes, uncapped, for a diagnosis running in process.
            #
            # R5-2: `top_identities` is a curated sample and the harness was grouping root
            # causes from it -- one live page has 33 unclassified leaves and this list carries ten, so
            # a "shared shape" only meant "shared among the first ten". Value-free by the same
            # projection as `identities`; `to_error_context` drops it before anything reaches a
            # CLI envelope, so the redaction a user sees is unchanged.
            #
            # The first attempt read `source.unclassified`, which `OwnershipProofSummary` does
            # not have -- so this was silently always empty and the harness kept sampling while
            # the code claimed it did not. `getattr` is what hid it; the field is required now,
            # and the test below pins that a summary without it is a failure rather than a
            # quietly shorter list.
            "all_identities": [_leaf_identity_context(identity) for identity in source.all_identities],
            "diagnostics": [
                {
                    "code": item.code,
                    "resolution": item.resolution,
                    "description": describe_migration_code(item.code),
                }
                for item in source.diagnostics
            ],
            # Static, value-free next-step: a failed in-place proof is not a dead
            # end — the append and single-leaf patch paths stay available.
            "supported_alternatives": list(SUPPORTED_ALTERNATIVES),
        }
    identities: list[dict[str, Any]] = []
    seen: set[bytes] = set()
    for key in ("unclassified", "multiple_owners", "overlap"):
        for item in source.get(key) or ():
            identity = item.get("identity") if isinstance(item, dict) else None
            if identity is None:
                continue
            projected = _leaf_identity_context(identity)
            marker = _canonical_json(projected)
            if marker in seen:
                continue
            seen.add(marker)
            identities.append(projected)
    codes = source.get("fatal_diagnostic_codes") or []
    fatal_class = codes[0] if codes else "fatal"
    return {
        "reason": reason,
        "fatal_class": fatal_class,
        "fatal_class_description": describe_migration_code(fatal_class),
        "counts": {key: len(source.get(key) or []) for key in ("unclassified", "multiple_owners", "overlap")},
        "identities": identities[:10],
        "diagnostic_codes": sorted(codes),
        "supported_alternatives": list(SUPPORTED_ALTERNATIVES),
    }


def storage_comparison_context(comparison: cfxmark.StorageComparison | None) -> dict[str, Any] | None:
    """Project a ``StorageComparison``'s value-free public summary to JSON, or ``None`` when equivalent."""

    if comparison is None or comparison.public_summary is None:
        return None
    summary = comparison.public_summary
    return {
        "code": summary.code,
        "identity": _leaf_identity_context(summary.identity),
        "policy": summary.policy,
        "resolution": summary.resolution,
    }


def _redact_occurrence_list(occurrences: Any) -> Any:
    if not isinstance(occurrences, list):
        return occurrences
    return [
        {key: value for key, value in occurrence.items() if key in _SAFE_OCCURRENCE_KEYS}
        if isinstance(occurrence, dict)
        else occurrence
        for occurrence in occurrences
    ]


def _redact_report(report: Any) -> Any:
    if not isinstance(report, dict) or not isinstance(report.get("occurrences"), list):
        return report
    return {**report, "occurrences": _redact_occurrence_list(report["occurrences"])}


def _redact_assets(assets: Any) -> Any:
    if not isinstance(assets, dict) or not isinstance(assets.get("items"), list):
        return assets
    items = [
        {key: value for key, value in item.items() if key in _SAFE_ASSET_ITEM_KEYS} if isinstance(item, dict) else item
        for item in assets["items"]
    ]
    return {**assets, "items": items}


def _redact_ownership(ownership: Any) -> Any:
    if not isinstance(ownership, dict):
        return ownership
    result = dict(ownership)
    # In-process only. The uncapped list exists so a harness can group root causes without
    # sampling; the envelope keeps the curated `identities`.
    result.pop("all_identities", None)
    for key in ("unclassified", "multiple_owners", "overlap"):
        items = result.get(key)
        if isinstance(items, list):
            result[key] = [
                _leaf_identity_context(item["identity"])
                for item in items
                if isinstance(item, dict) and "identity" in item
            ]
    return result


def to_error_context(context: dict[str, Any]) -> dict[str, Any]:
    """Return a value-free copy of an error/consent context.

    Redacts report occurrences (message text, before/after summaries, fingerprints,
    asset names), deferred-migration occurrences, asset-plan items (the portable
    ``src`` path and server ``remote_name`` attachment filename), ownership
    fatal-group leaf dumps, and source-conversion loss messages, keeping only
    canonical identity, counts, codes, hashes, and resolutions. Every other field
    (status, sha256 digests, fingerprints used as approval tokens, next_actions) is
    preserved unchanged.
    """

    result = dict(context)
    result.pop("all_identities", None)
    for key in ("migration_report", "source_conversion_report"):
        if key in result:
            result[key] = _redact_report(result[key])
    if isinstance(result.get("deferred_migrations"), list):
        result["deferred_migrations"] = _redact_occurrence_list(result["deferred_migrations"])
    if isinstance(result.get("assets"), dict):
        result["assets"] = _redact_assets(result["assets"])
    if isinstance(result.get("ownership"), dict):
        result["ownership"] = _redact_ownership(result["ownership"])
    conversion = result.get("conversion")
    if isinstance(conversion, dict) and isinstance(conversion.get("losses"), list):
        result["conversion"] = {**conversion, "losses": []}
    return result


def append_proof_sha256(
    *,
    site: str,
    page_id: str,
    remote_version: int,
    remote_storage_sha256: str,
    converter: str,
    profile: str,
    passthrough: tuple[str, ...],
    base_markdown_sha256: str,
    edited_markdown_sha256: str,
    fragment_markdown_sha256: str,
    fragment_storage_sha256: str,
    candidate_storage_sha256: str,
    asset_plan_sha256: str,
) -> str:
    payload = {
        "schema": "atls-exact-eof-append-v1",
        "site": site,
        "page": page_id,
        "remote_version": remote_version,
        "remote_storage_sha256": remote_storage_sha256,
        "converter": converter,
        "profile": profile,
        "passthrough": serialize_passthrough(passthrough),
        "base_markdown_sha256": base_markdown_sha256,
        "edited_markdown_sha256": edited_markdown_sha256,
        "fragment_markdown_sha256": fragment_markdown_sha256,
        "fragment_storage_sha256": fragment_storage_sha256,
        "candidate_storage_sha256": candidate_storage_sha256,
        "asset_plan_sha256": asset_plan_sha256,
    }
    return _sha256_bytes(_canonical_json(payload), prefix="append_sha256")


_APPEND_VIEW_COMMENT_RE = re.compile(r"<!-- (?:atls:(?:managed|operation)|cfxmark:[a-z-]+)\b.*? -->")
# The opaque payload sidecar is a multi-line section; it must be extracted as
# one unit before the single-line comment pass (mirrors cfxmark.opaque).
_APPEND_VIEW_PAYLOADS_RE = re.compile(
    re.escape("<!-- cfxmark:payloads -->") + r".*?" + re.escape("<!-- /cfxmark:payloads -->"),
    re.DOTALL,
)


def _append_comment_position_view(text: str) -> tuple[str, tuple[str, ...]]:
    """Projection equality for the exact-append proof, comment-position-safe.

    Pull re-emits control comments (cfxmark:align / cfxmark:payloads /
    migration blocks) at normalized positions, so a byte-equality check makes
    appending after such a comment fail eligibility even though the append is
    loss-free. The proof instead requires the exact same comment multiset AND byte-identical
    remaining content — position is the only degree of freedom granted.
    """

    sections = tuple(_APPEND_VIEW_PAYLOADS_RE.findall(text))
    text = _APPEND_VIEW_PAYLOADS_RE.sub("", text)
    comments = tuple(sorted((*sections, *_APPEND_VIEW_COMMENT_RE.findall(text))))
    stripped = _APPEND_VIEW_COMMENT_RE.sub("", text)
    stripped = re.sub(r"[ \t]+\n", "\n", stripped)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip("\n") + "\n"
    return stripped, comments


def _append_candidate(
    *,
    source_storage: str,
    base_markdown: str,
    edited_markdown: str,
    options: cfxmark.ConversionOptions,
    site: str,
    page_id: str,
    remote_version: int,
    asset_plan_sha256: str,
    passthrough: tuple[str, ...],
    source_references: dict[str, str],
) -> tuple[str, str, str] | None:
    prefix = base_markdown + "\n"
    if not edited_markdown.startswith(prefix):
        return None
    fragment = edited_markdown[len(prefix) :]
    if not fragment.strip():
        return None
    fragment_artifact = cfxmark.to_cfx_artifact(fragment, options=options)
    if not fragment_artifact.push_safe or fragment_artifact.attachments or fragment_artifact.presentation.cells:
        return None
    if any(diagnostic.blocking for diagnostic in fragment_artifact.diagnostics):
        return None
    candidate_storage = source_storage + fragment_artifact.xhtml
    projected = cfxmark.to_md_artifact(candidate_storage, options=options)
    if source_references:
        projected = rewrite_attachment_artifact(projected, source_references)
    if _append_comment_position_view(canonical_managed_content(projected.markdown)) != _append_comment_position_view(
        edited_markdown
    ):
        return None
    append_sha256 = append_proof_sha256(
        site=site,
        page_id=page_id,
        remote_version=remote_version,
        remote_storage_sha256=_remote_storage_hash(source_storage),
        converter=f"cfxmark/{cfxmark.__version__}",
        profile="markdown-first",
        passthrough=passthrough,
        base_markdown_sha256=_sha256_text(base_markdown),
        edited_markdown_sha256=_sha256_text(edited_markdown),
        fragment_markdown_sha256=_sha256_text(fragment),
        fragment_storage_sha256=_sha256_text(fragment_artifact.xhtml),
        candidate_storage_sha256=_sha256_text(candidate_storage),
        asset_plan_sha256=asset_plan_sha256,
    )
    return candidate_storage, append_sha256, _sha256_text(fragment)


def _merge_outlook(
    local_path: Path,
    page_id: str,
    source_storage: str,
    document: ManagedDocument,
) -> dict[str, Any]:
    """Whether the local edit and the remote change can be combined, as reported detail.

    Answers the question, never acts on it. A stale push that quietly merged and
    published would be doing something the caller did not ask for, on a page they
    have not seen since it moved.

    Every outcome is named, including the ones where the answer is "cannot tell".
    A silent omission here reads as "no merge possible", and a caller would redo
    an edit by hand that we could have combined in one step.
    """

    from atlassian_skills.confluence.merge import merge3
    from atlassian_skills.confluence.sidecar import SidecarUnusable, read_sidecar

    # Reported alongside every outcome that a merge could resolve. Saying "these
    # combine" and leaving the caller to invent the command is the dead end this
    # set out to remove, one step further along.
    prepare = {
        "label": "lay out base, local and remote so the merge can be done and checked",
        "argv": [
            "confluence",
            "page",
            "md",
            "prepare-merge",
            page_id,
            "--md-file",
            str(local_path),
            "--format=json",
        ],
        "requires_user_approval": False,
    }

    try:
        sidecar = read_sidecar(local_path, page_id=page_id)
    except SidecarUnusable as unusable:
        # Not an error: pulls made before sidecars existed have none, and the
        # push itself is unaffected. Only the merge is unavailable, and saying
        # which is the difference between a limitation and a mystery.
        return {"merge_available": False, "merge_unavailable_reason": unusable.reason}

    try:
        remote_markdown = cfxmark.to_md_artifact(
            source_storage,
            options=cfxmark.ConversionOptions(
                profile="editable",
                passthrough_html_comment_prefixes=document.manifest.passthrough,
            ),
        ).markdown
    except Exception as error:  # noqa: BLE001 - an unconvertible remote is a report, not a crash
        return {"merge_available": False, "merge_unavailable_reason": f"remote_unconvertible:{type(error).__name__}"}

    # The header notice is a banner atls and cfxmark own, not something an author
    # wrote, and only two of the three sides carry it. Left in, it reads as an
    # edit the remote made to every document and turns every merge into a
    # conflict about a sentence nobody typed.
    strip = cfxmark.strip_header_notice
    result = merge3(strip(sidecar.base_markdown), strip(document.content), strip(remote_markdown))
    if result.clean:
        return {"merge_available": True, "merge_conflicts": 0, "next_actions": [prepare]}
    # A conflict still goes to prepare-merge. The three files and the conflict
    # locations are exactly what resolving one needs, and the line merger calls
    # things conflicts that a reader settles in a moment.
    return {
        "merge_available": False,
        "merge_unavailable_reason": "conflict",
        "merge_conflicts": len(result.conflicts),
        "next_actions": [prepare],
    }


def build_managed_preflight(
    client: Any,
    page_id: str,
    managed_path: Path,
    *,
    passthrough_prefixes: tuple[str, ...] | None = None,
    recovery_operation: ManagedOperation | None = None,
    recovery_assets: tuple[ManagedAssetOperation, ...] = (),
) -> ManagedPreflight:
    identity_before = inspect_file_identity(managed_path).key
    managed = read_managed_utf8(managed_path)
    identity_after = inspect_file_identity(managed_path).key
    if identity_after != identity_before:
        raise ValidationError(
            "Managed Markdown changed while preflight was reading it",
            context={"reason": "local_changed_during_preflight"},
        )
    records = extract_asset_records(managed)
    try:
        document = parse_managed_document(managed, assets=records, verify_content=False, verify_assets=False)
    except ManagedManifestError as error:
        if "legacy" in error.reason:
            raise ValidationError(
                "Legacy managed Markdown must be re-pulled into the portable v2 format. "
                "Preserve your local edits, pull a fresh copy to a separate safe path with "
                "'atls confluence page pull-md', then reapply the intended edits.",
                context={
                    **error.context,
                    "reason": "legacy_manifest_repull_required",
                    "path": str(managed_path),
                    "page_id": page_id,
                },
            ) from error
        raise ValidationError(
            "Managed Markdown manifest is invalid",
            context=error.context,
        ) from error
    manifest = document.manifest
    managed_file_sha256 = _sha256_text(managed)
    managed_content_sha256 = canonical_content_sha256(document.content)
    managed_assets_sha256 = canonical_asset_set_sha256(records)
    effective_site_url = getattr(client, "base_url", None)
    if not isinstance(effective_site_url, str):
        raise ValidationError("Managed push requires the configured Confluence base URL")
    site = site_fingerprint(effective_site_url)
    if manifest.page != page_id or manifest.site != site:
        raise ValidationError(
            "Managed Markdown targets a different page or site",
            context={"reason": "managed_authority_mismatch"},
        )
    # A page whose losses could not be classified is edited as storage, and while
    # it is, this file is a reading copy. Publishing it would write back a
    # Markdown rendering of a document Markdown was found unable to hold -- which
    # is the loss this whole path exists to refuse, arriving by the other door.
    if read_authority(managed_path) == "xhtml":
        raise ValidationError(
            "This page is published as storage, so the Markdown copy is read-only.",
            hint=(
                "Edit the storage document and publish with 'atls confluence page push-xhtml', or "
                "hand authority back with 'atls confluence page set-authority --to=markdown'."
            ),
            context={"reason": "xhtml_is_authoritative", "page_id": page_id, "path": str(managed_path)},
        )
    expected_converter = f"cfxmark/{cfxmark.__version__}"
    if manifest.converter != expected_converter or manifest.profile != "markdown-first":
        raise ValidationError(
            "Managed Markdown converter/profile is not current",
            context={"reason": "managed_converter_mismatch"},
            hint=MANAGED_CONVERTER_HINT,
        )
    if passthrough_prefixes is not None:
        supplied = parse_passthrough(serialize_passthrough(passthrough_prefixes))
        if supplied != manifest.passthrough:
            raise ValidationError(
                "--passthrough-prefix does not match the managed manifest",
                context={"reason": "passthrough_mismatch"},
            )

    page = client.get_page(page_id)
    source_storage = page.body_storage or ""
    remote_version = _page_version(page)
    remote_storage_sha256 = _remote_storage_hash(source_storage)
    if (remote_version, remote_storage_sha256) != (manifest.remote_version, manifest.remote_storage):
        # Correct, and on its own a dead end. Measured across 55 live pages, every
        # managed push against a page someone else had touched landed here, with
        # nothing to do but pull again and redo the edit by hand -- which is what
        # sends people back to the browser. Most of those are not conflicts at
        # all: a typo fixed three sections from the paragraph being edited.
        #
        # So the refusal now says whether the two edits can be combined. It still
        # refuses; it just stops being the end of the road.
        raise StaleError(
            "Managed Markdown baseline is stale",
            context={
                "reason": "remote_stale",
                "expected_version": manifest.remote_version,
                "server_version": remote_version,
                **_merge_outlook(managed_path, page_id, source_storage, document),
            },
        )

    if recovery_operation is None:
        if recovery_assets:
            raise ValidationError(
                "Recovery assets require an operation journal",
                context={"reason": "asset_operation_missing"},
            )
        asset_plan, asset_plan_sha256, asset_dirty = _asset_plan(
            client,
            page_id,
            managed_path,
            document.content,
            records,
        )
    else:
        if any(asset.operation_id != recovery_operation.operation_id for asset in recovery_assets):
            raise ValidationError(
                "Asset receipts do not belong to the pending operation",
                context={"reason": "asset_operation_id_mismatch"},
            )
        asset_plan, asset_plan_sha256, asset_dirty = rebuild_managed_asset_plan(managed_path, recovery_assets)
        if asset_plan_sha256 != recovery_operation.assets:
            raise ValidationError(
                "Pending asset receipts do not match the operation plan",
                context={"reason": "asset_operation_plan_mismatch"},
            )
    options = cfxmark.ConversionOptions(
        profile="editable",
        passthrough_html_comment_prefixes=manifest.passthrough,
        attachment_filename_map=tuple(
            (item.src, item.remote_name) for item in asset_plan if item.src and item.action != "unreferenced"
        ),
    )
    proof_base_artifact = cfxmark.to_md_artifact(source_storage, options=options)
    base_artifact = proof_base_artifact
    source_references = {record.remote_name: record.src for record in records}
    source_references.update(
        {asset.remote_name: asset.src for asset in recovery_assets if asset.baseline_id is not None}
    )
    if source_references:
        base_artifact = rewrite_attachment_artifact(
            base_artifact,
            source_references,
        )
    base_markdown = canonical_managed_content(base_artifact.markdown)
    edited_markdown = canonical_managed_content(document.content)
    report = base_artifact.migration_report
    report_payload = _migration_report_payload(report, display=True)
    report_sha256 = _report_hash(report)
    deferred = tuple(report_payload["occurrences"])
    empty_ownership: dict[str, Any] = {
        "intended_operation_ids": [],
        "accepted_migration_occurrence_ids": [],
        "unclassified": [],
        "multiple_owners": [],
        "overlap": [],
        "fatal_diagnostic_codes": [],
    }

    if edited_markdown == base_markdown and not asset_dirty:
        return ManagedPreflight(
            proof_mode="no_change",
            status="no_change",
            page_id=page_id,
            site=site,
            managed_file_identity=identity_after,
            managed_file_sha256=managed_file_sha256,
            managed_content_sha256=managed_content_sha256,
            managed_assets_sha256=managed_assets_sha256,
            remote_version=remote_version,
            remote_storage_sha256=remote_storage_sha256,
            body_dirty=False,
            asset_dirty=False,
            candidate_storage_sha256=remote_storage_sha256,
            migration_report=report_payload,
            migration_report_sha256=report_sha256,
            migration_fingerprint=None,
            append_sha256=None,
            append_fragment_sha256=None,
            consent_required=False,
            asset_plan_sha256=asset_plan_sha256,
            asset_plan=asset_plan,
            ownership=empty_ownership,
            deferred_migrations=deferred,
            candidate_storage=source_storage,
            source_storage=source_storage,
            base_markdown=base_markdown,
            edited_markdown=edited_markdown,
            document=document,
            candidate=None,
            managed_path=str(managed_path),
        )

    if edited_markdown == base_markdown and asset_dirty:
        return ManagedPreflight(
            proof_mode="full_migration",
            status="ready_to_publish",
            page_id=page_id,
            site=site,
            managed_file_identity=identity_after,
            managed_file_sha256=managed_file_sha256,
            managed_content_sha256=managed_content_sha256,
            managed_assets_sha256=managed_assets_sha256,
            remote_version=remote_version,
            remote_storage_sha256=remote_storage_sha256,
            body_dirty=False,
            asset_dirty=True,
            candidate_storage_sha256=remote_storage_sha256,
            migration_report=report_payload,
            migration_report_sha256=report_sha256,
            migration_fingerprint=None,
            append_sha256=None,
            append_fragment_sha256=None,
            consent_required=False,
            asset_plan_sha256=asset_plan_sha256,
            asset_plan=asset_plan,
            ownership=empty_ownership,
            deferred_migrations=deferred,
            candidate_storage=source_storage,
            source_storage=source_storage,
            base_markdown=base_markdown,
            edited_markdown=edited_markdown,
            document=document,
            candidate=None,
            managed_path=str(managed_path),
        )

    if not asset_dirty:
        append = _append_candidate(
            source_storage=source_storage,
            base_markdown=base_markdown,
            edited_markdown=edited_markdown,
            options=options,
            site=site,
            page_id=page_id,
            remote_version=remote_version,
            asset_plan_sha256=asset_plan_sha256,
            passthrough=manifest.passthrough,
            source_references=source_references,
        )
        if append is not None:
            candidate_storage, append_sha256, fragment_sha256 = append
            return ManagedPreflight(
                proof_mode="exact_remote_prefix_append",
                status="ready_to_publish",
                page_id=page_id,
                site=site,
                managed_file_identity=identity_after,
                managed_file_sha256=managed_file_sha256,
                managed_content_sha256=managed_content_sha256,
                managed_assets_sha256=managed_assets_sha256,
                remote_version=remote_version,
                remote_storage_sha256=remote_storage_sha256,
                body_dirty=True,
                asset_dirty=False,
                candidate_storage_sha256=_sha256_text(candidate_storage),
                migration_report=report_payload,
                migration_report_sha256=report_sha256,
                migration_fingerprint=None,
                append_sha256=append_sha256,
                append_fragment_sha256=fragment_sha256,
                consent_required=False,
                asset_plan_sha256=asset_plan_sha256,
                asset_plan=asset_plan,
                ownership=empty_ownership,
                deferred_migrations=deferred,
                candidate_storage=candidate_storage,
                source_storage=source_storage,
                base_markdown=base_markdown,
                edited_markdown=edited_markdown,
                document=document,
                candidate=None,
                managed_path=str(managed_path),
            )

    proof_filename_map = {
        item.src: item.remote_name for item in asset_plan if item.src and item.action not in {"create", "unreferenced"}
    }
    proof_edited_markdown = bind_managed_attachment_markdown(edited_markdown, proof_filename_map)
    compatibility = compatibility_payload(
        page_id,
        source_storage,
        document_path=str(managed_path),
        base_artifact=proof_base_artifact,
    )
    if compatibility["preservation_capability"] == "ragged-table-island-v1":
        protected_paths = ragged_protected_table_paths(proof_base_artifact)
        migration_base = replace(
            proof_base_artifact,
            protected_regions=tuple(
                region
                for region in proof_base_artifact.protected_regions
                if tuple(region.remote_node_path) in protected_paths
            ),
            remote_subtrees=tuple(
                subtree
                for subtree in proof_base_artifact.remote_subtrees
                if tuple(subtree.remote_node_path) in protected_paths
            ),
        )
    else:
        migration_base = replace(proof_base_artifact, protected_regions=(), remote_subtrees=())
    candidate = cfxmark.to_cfx_artifact(
        proof_edited_markdown,
        presentation=migration_base.presentation,
        base_artifact=migration_base,
        splice_source=source_storage,
        options=options,
    )
    if candidate.protected_region_changes:
        raise ValidationError(
            "The edit changes a remote-backed structure that this Markdown file may only preserve.",
            hint="Leave the protected table unchanged and edit prose outside it.",
            context={
                "reason": "protected_region_edited",
                "diagnostic_code": "protected-region-edited",
                "count": len(candidate.protected_region_changes),
            },
        )
    # Measured on the candidate rather than assumed from the classification, and
    # computed before the proof runs so both refusals below can consult one
    # answer. A page that holds nothing Markdown cannot express, whose candidate
    # is byte-identical to a plain render of the edited Markdown, has nothing
    # bound to the remote for an attribution to get wrong.
    mootness: Mootness | None = None
    proof_waived = False
    try:
        cfxmark.validate_managed_cfx_artifact(
            candidate,
            source_storage=source_storage,
            options=options,
        )
    except cfxmark.OwnershipProofError as error:
        mootness = assess_proof_mootness(
            source_storage,
            candidate.xhtml,
            proof_edited_markdown,
            options=options,
        )
        if not mootness.moot:
            raise ValidationError(
                "Managed candidate ownership proof is not publishable",
                # Carried onto the refusal, not just onto success. The proof
                # refuses exactly the pages whose diagnosis matters most, and
                # dropping the assessment here leaves the caller with a failure
                # and no account of what the page actually holds.
                context={
                    **ownership_error_context(error.summary, reason="ownership_proof_invalid"),
                    # The value-free projection, not the full payload: error
                    # contexts are displayed and logged, and this project denies
                    # leaf values crossing that boundary by default. A finding
                    # code is a name we chose and a count is an integer; neither
                    # says what the page says.
                    "compatibility": compatibility_digest(page_id, source_storage),
                    "proof_mootness": mootness.reason,
                    "next_actions": [
                        {
                            "label": "compare the local edit with its base and the current page",
                            "argv": [
                                "confluence",
                                "page",
                                "md",
                                "compare",
                                page_id,
                                "--md-file",
                                str(managed_path),
                                "--view=diff",
                                "--format=json",
                            ],
                            "requires_user_approval": False,
                        },
                        {
                            "label": "lay out base, local and remote copies for reconciliation",
                            "argv": [
                                "confluence",
                                "page",
                                "md",
                                "prepare-reconcile",
                                page_id,
                                "--md-file",
                                str(managed_path),
                                "--output-dir",
                                f"{managed_path}.reconcile",
                                "--format=json",
                            ],
                            "requires_user_approval": False,
                        },
                    ],
                },
                hint=OWNERSHIP_PROOF_HINT,
            ) from error
        proof_waived = True
    except (cfxmark.CfxmarkError, TypeError, ValueError) as error:
        # Not covered by mootness on purpose. A converter that failed outright
        # produced no candidate to check, so there is nothing to have verified.
        raise ValidationError(
            "Managed candidate ownership proof is not publishable",
            context={"reason": "ownership_proof_invalid", **conversion_failure_context(error)},
        ) from error
    ownership = _ownership_payload(candidate)
    if any(ownership[key] for key in ("unclassified", "multiple_owners", "overlap", "fatal_diagnostic_codes")):
        if mootness is None:
            mootness = assess_proof_mootness(
                source_storage,
                candidate.xhtml,
                proof_edited_markdown,
                options=options,
            )
        if not mootness.moot:
            raise ValidationError(
                "Managed candidate ownership proof is incomplete or ambiguous",
                context={
                    **ownership_error_context(ownership, reason="ownership_proof_fatal"),
                    "proof_mootness": mootness.reason,
                },
                hint=OWNERSHIP_PROOF_HINT,
            )
        proof_waived = True
    if mootness is None:
        classification = str(compatibility["classification"])
        mootness = (
            Mootness(False, "page_is_not_markdown_lossless", classification)
            if classification != cfxmark.MARKDOWN_LOSSLESS
            else assess_proof_mootness(
                source_storage,
                candidate.xhtml,
                proof_edited_markdown,
                options=options,
            )
        )
    # Reported whether or not it fired, so a run over a corpus can count how
    # often attribution actually decided anything. A flag that only appears when
    # it is true reads as absent rather than as false.
    ownership = {**ownership, "proof_waived": proof_waived, "proof_mootness": mootness.to_dict()}

    candidate_report = candidate.source_migration_report or report
    accepted_ids = frozenset(ownership["accepted_migration_occurrence_ids"])
    # Asked of cfxmark's registry rather than decided here, so "between ordinary body
    # blocks" has exactly one definition. Read from the source storage, because the
    # question is what the stored page holds -- what the candidate did to it is the
    # thing being classified, not the thing that licenses the classification.
    canonicalized_sites = canonicalization_sites(source_storage)
    report_payload = _migration_report_payload(
        candidate_report, display=True, occurrence_ids=accepted_ids, canonicalized_sites=canonicalized_sites
    )
    report_sha256 = _report_hash(candidate_report, occurrence_ids=accepted_ids, canonicalized_sites=canonicalized_sites)
    consent_required = _consent_required(
        candidate_report, occurrence_ids=accepted_ids, canonicalized_sites=canonicalized_sites
    )
    loss_payload = candidate_loss(source_storage, candidate.xhtml)
    candidate_storage_sha256 = (
        candidate.candidate_storage_sha256
        if candidate.candidate_storage_sha256.startswith("sha256:")
        else f"sha256:{candidate.candidate_storage_sha256}"
    )
    fingerprint_payload = {
        "schema": "atls-migration-fingerprint-v1",
        "site": site,
        "page": page_id,
        "remote_version": remote_version,
        "remote_storage_sha256": remote_storage_sha256,
        "converter": expected_converter,
        "profile": "markdown-first",
        "base_markdown_sha256": _sha256_text(base_markdown),
        "edited_markdown_sha256": _sha256_text(edited_markdown),
        "candidate_storage_sha256": candidate_storage_sha256,
        "asset_plan_sha256": asset_plan_sha256,
        "migration_report_sha256": report_sha256,
    }
    migration_fingerprint = _sha256_bytes(_canonical_json(fingerprint_payload), prefix="mig_sha256")
    return ManagedPreflight(
        proof_mode="regeneration_verified" if proof_waived else "full_migration",
        status="migration_consent_required" if consent_required else "ready_to_publish",
        page_id=page_id,
        site=site,
        managed_file_identity=identity_after,
        managed_file_sha256=managed_file_sha256,
        managed_content_sha256=managed_content_sha256,
        managed_assets_sha256=managed_assets_sha256,
        remote_version=remote_version,
        remote_storage_sha256=remote_storage_sha256,
        body_dirty=candidate.xhtml != source_storage,
        asset_dirty=asset_dirty,
        candidate_storage_sha256=candidate_storage_sha256,
        migration_report=report_payload,
        migration_report_sha256=report_sha256,
        migration_fingerprint=migration_fingerprint,
        append_sha256=None,
        append_fragment_sha256=None,
        consent_required=consent_required,
        asset_plan_sha256=asset_plan_sha256,
        asset_plan=asset_plan,
        ownership=ownership,
        deferred_migrations=tuple(
            item
            for item in _migration_report_payload(candidate_report, display=True)["occurrences"]
            if item["occurrence_id"] not in accepted_ids
        ),
        candidate_storage=candidate.xhtml,
        source_storage=source_storage,
        base_markdown=base_markdown,
        edited_markdown=edited_markdown,
        document=document,
        candidate=candidate,
        managed_path=str(managed_path),
        compatibility=compatibility,
        candidate_loss_payload=loss_payload,
    )


__all__ = [
    "MIGRATION_CODE_DESCRIPTIONS",
    "ManagedAssetPlanItem",
    "ManagedPreflight",
    "append_proof_sha256",
    "build_managed_preflight",
    "conversion_failure_code",
    "conversion_failure_context",
    "describe_migration_code",
    "ownership_error_context",
    "rebuild_managed_asset_plan",
    "storage_comparison_context",
    "to_error_context",
]
