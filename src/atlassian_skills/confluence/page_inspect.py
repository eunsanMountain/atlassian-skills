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
    return result
