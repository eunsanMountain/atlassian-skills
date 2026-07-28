"""Read-only Confluence page workflow inspection."""

from __future__ import annotations

from typing import Any

import cfxmark

from atlassian_skills.confluence.migration_preflight import (
    _migration_report_payload,
    _report_hash,
    conversion_failure_context,
)
from atlassian_skills.core.errors import ValidationError

INTENTS = frozenset({"read", "text-edit", "append", "structure-edit", "presentation-edit"})
# Intents that imply replacing existing content in place, and therefore depend on the
# in-place proof holding. `read` writes nothing and `append` has its own exact-append
# path, so neither pays for the prediction.
_IN_PLACE_INTENTS = frozenset({"text-edit", "structure-edit", "presentation-edit"})


def _page_version(page: Any) -> int:
    version = getattr(page, "version", None)
    return int(getattr(version, "number", version) or 1)


def _recommendation(intent: str) -> tuple[str, str | None, list[dict[str, str]]]:
    if intent == "read":
        return "page-get-md", None, []
    if intent == "text-edit":
        return (
            "patch-text",
            None,
            [
                {"workflow": "pull-md", "reason": "use for structural edits or text spanning storage leaves"},
                {"workflow": "web-editor", "reason": "use when unsupported rich content itself must change"},
            ],
        )
    if intent == "append":
        return (
            "pull-md",
            "exact_remote_prefix_append",
            [
                {
                    "workflow": "push-md-dry-run",
                    "reason": "authoritatively checks whether the local edit is an exact safe EOF append",
                }
            ],
        )
    if intent == "structure-edit":
        return (
            "pull-md",
            None,
            [
                {"workflow": "patch-text", "reason": "preserve rich storage for a small text-only edit"},
                {"workflow": "web-editor", "reason": "preserve unsupported presentation while editing it"},
            ],
        )
    return (
        "web-editor",
        None,
        [{"workflow": "pull-md", "reason": "use when the intended presentation is representable in managed Markdown"}],
    )


def inspect_page(client: Any, page_id: str, *, intent: str) -> dict[str, Any]:
    """Return a read-only recommendation; never create managed state or files."""

    if intent not in INTENTS:
        raise ValidationError(
            "--intent must be read, text-edit, append, structure-edit, or presentation-edit",
            context={"reason": "invalid_page_inspect_intent", "intent": intent},
        )
    page = client.get_page(page_id)
    storage = getattr(page, "body_storage", None)
    if not isinstance(storage, str):
        raise ValidationError("Confluence page storage body is missing", context={"reason": "storage_missing"})
    try:
        artifact = cfxmark.to_md_artifact(storage, options=cfxmark.ConversionOptions(profile="editable"))
    except cfxmark.CfxmarkError as error:
        raise ValidationError(
            "Confluence storage could not be inspected safely",
            context={"reason": "page_inspect_conversion_failed", **conversion_failure_context(error)},
        ) from error
    report = artifact.migration_report
    counts = report.counts()
    tables = sum(node.node_type == "Table" for node in artifact.semantic_nodes)
    macros = sum(
        bool(item.remote_node_path) and item.remote_node_path[0].startswith("ac:structured-macro[")
        for item in artifact.remote_subtrees
    )
    styled_cells = sum(cell.background is not None for cell in artifact.presentation.cells)
    # `styled_cells` counts background colour only. Alignment lives in the same
    # presentation channel but leaves `background` unset, so a page whose tables are
    # entirely alignment-styled used to report `styled_cells: 0` — a green light for
    # exactly the pages an in-place table edit can fail on. Count it separately
    # rather than folding it in, so the existing field keeps its meaning.
    aligned_cells = sum(
        cell.background is None and cell.source_style is not None for cell in artifact.presentation.cells
    )
    # Table presentation is re-attached by table identity. Two tables of the same
    # shape are what makes that identity ambiguous, so surface the count an operator
    # would otherwise have to derive by hand.
    shapes: dict[str, int] = {}
    for table in artifact.presentation.tables:
        shapes[table.topology_hash] = shapes.get(table.topology_hash, 0) + 1
    duplicate_table_shapes = sum(count for count in shapes.values() if count > 1)
    attachments = client.list_attachments(page_id)
    recommended, preferred_proof, alternatives = _recommendation(intent)
    result: dict[str, Any] = {
        "status": "inspected",
        "page_id": page_id,
        "version": _page_version(page),
        "intent": intent,
        "recommended_workflow": recommended,
        "features": {
            "tables": tables,
            "styled_cells": styled_cells,
            "aligned_cells": aligned_cells,
            "duplicate_table_shapes": duplicate_table_shapes,
            "macros": macros,
            "attachments": len(attachments),
        },
        "migration": {
            "consent_required": report.consent_required,
            "normalized": counts.get("normalized", 0),
            "converted": counts.get("converted", 0),
            "removed": counts.get("removed", 0),
            "unsupported": counts.get("unsupported", 0),
            "report_sha256": _report_hash(report),
            "report": _migration_report_payload(report, display=True),
        },
        "alternatives": alternatives,
        "managed_artifact_created": False,
        "authoritative_write_preflight": False,
    }
    if preferred_proof is not None:
        result["preferred_proof"] = preferred_proof
    if intent in _IN_PLACE_INTENTS:
        # `recommended_workflow` says which tool to reach for; it does not say whether
        # this page can actually be edited in place. Pull already answers that by
        # running one no-edit proof, but an operator deciding *before* pulling only
        # has inspect. Run the same prediction here so the two agree.
        #
        # Scoped to in-place intents on purpose: the proof is O(page size) and is
        # irrelevant to `read` (no write) and `append` (its own exact-append path).
        from atlassian_skills.confluence.managed_pull import (
            _predict_in_place_editability,
            in_place_edit_guidance,
        )

        # No passthrough prefixes: the artifact above was built with the same plain
        # editable options, so the prediction must run against the same conversion.
        predicted = in_place_edit_guidance(*_predict_in_place_editability(artifact, storage, ()))
        if predicted is not None:
            result["edit_guidance"] = [predicted]
    return result
