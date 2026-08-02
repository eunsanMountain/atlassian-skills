from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any, Literal

from atlassian_skills.core.errors import ValidationError

ProofMode = Literal["exact_remote_prefix_append", "full_migration", "full_replacement"]


def _proof_prefix(fingerprint: str) -> str:
    prefix, separator, digest = fingerprint.partition(":")
    if not separator or not prefix or len(digest) < 12:
        raise ValidationError(
            "Confluence version reason requires a valid proof fingerprint",
            context={"reason": "version_reason_fingerprint_invalid"},
        )
    return digest[:12]


def _migration_summary(report: Mapping[str, Any]) -> str:
    occurrences = report.get("occurrences", ())
    effects = Counter(
        item.get("effect") for item in occurrences if isinstance(item, Mapping) and isinstance(item.get("effect"), str)
    )
    ordered = tuple(
        f"{effect}={effects[effect]}"
        for effect in ("removed", "converted", "unsupported", "normalized")
        if effects[effect]
    )
    return ",".join(ordered) if ordered else "reported=0"


def proof_bound_version_reason(
    *,
    proof_mode: ProofMode,
    fingerprint: str,
    migration_report: Mapping[str, Any],
    user_reason: str | None,
) -> str:
    """Return a deterministic, proof-bound Confluence version message."""

    proof = _proof_prefix(fingerprint)
    if proof_mode == "exact_remote_prefix_append":
        generated = f"atls exact EOF append {proof}"
    elif proof_mode == "full_replacement":
        generated = f"atls full replacement {proof}: {_migration_summary(migration_report)}"
    else:
        generated = f"atls markdown migration {proof}: {_migration_summary(migration_report)}"
    return f"{generated}; {user_reason}" if user_reason is not None else generated


__all__ = ["proof_bound_version_reason"]
