"""Fail-closed plain-text leaf patching for Confluence storage fragments."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import cfxmark
import lxml.etree as ET  # type: ignore[import-untyped]

from atlassian_skills.core.errors import ConflictError, StaleError, ValidationError

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]*")
_ATTR_RE = re.compile(r"""([A-Za-z_][A-Za-z0-9_.:-]*)\s*=\s*("([^"]*)"|'([^']*)')""")
_EXCLUDED_ANCESTORS = {"parameter", "plain-text-body"}

# The nearest one of these is the block a visible text run belongs to. Two
# leaves are only "adjacent" if they share a block, so a --find that spans two
# paragraphs, two table cells or two list items is not a boundary match — it is
# text that does not exist anywhere.
_BLOCK_ELEMENTS = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th", "pre", "blockquote"})

# A visible run ends here even inside one block: a hard break renders as a line
# break, and macro content is not plain prose the user can patch.
_RUN_BREAK_ELEMENTS = frozenset({"br", "hr"})
_WRAPPER_PREFIX = (
    '<root xmlns:ac="http://atlassian.com/content" '
    'xmlns:ri="http://atlassian.com/resource/identifier" '
    'xmlns:at="http://atlassian.com/template">'
)


class PatchTextClient(Protocol):
    def get_page(self, page_id: str) -> Any: ...

    def update_page(
        self,
        page_id: str,
        title: str,
        body: str,
        version_number: int,
        body_format: str = "storage",
        *,
        reason: str | None = None,
        minor_edit: bool = False,
    ) -> dict[str, Any]: ...


@dataclass
class _Frame:
    local_name: str
    ordinal: int
    text_ordinal: int = 0
    child_ordinals: dict[str, int] = field(default_factory=dict)
    # Bumped whenever something ends the visible run inside this element, so
    # two leaves separated by <br/> never join.
    run_index: int = 0


@dataclass(frozen=True)
class _TextSegment:
    start: int
    end: int
    raw: str
    decoded: str
    node_path: str
    eligible: bool
    # Identity of the visible run this leaf belongs to. Only leaves sharing a
    # run may be concatenated when looking for a boundary-spanning match.
    block_path: str = ""
    run_index: int = 0
    # "text" for ordinary character data, "cdata" and "attribute" for content
    # that is visible in the storage bytes but can never be a patch target.
    kind: str = "text"


@dataclass(frozen=True)
class PatchSelector:
    node_path: str
    before_fingerprint: str
    after_text: str
    before_text: str | None = None


@dataclass(frozen=True)
class PatchDocument:
    version: int
    changes: tuple[PatchSelector, ...]


@dataclass(frozen=True)
class AppliedPatch:
    node_path: str
    before_fingerprint: str
    before_text: str
    after_text: str


@dataclass(frozen=True)
class PatchCandidate:
    storage: str
    changes: tuple[AppliedPatch, ...]


def node_fingerprint(node_path: str, text: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"atls.patch-text.node.v1\0")
    digest.update(node_path.encode("utf-8"))
    digest.update(b"\0")
    digest.update(text.encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def parse_patch_document(payload: object) -> PatchDocument:
    if not isinstance(payload, dict) or set(payload) != {"version", "changes"}:
        raise ValidationError(
            "patch file must contain exactly version and changes",
            context={"reason": "patch_file_schema_invalid"},
        )
    version = payload.get("version")
    changes = payload.get("changes")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValidationError(
            "patch file version must be a positive integer",
            context={"reason": "patch_file_schema_invalid", "field": "version"},
        )
    if not isinstance(changes, list) or not changes:
        raise ValidationError(
            "patch file changes must be a non-empty array",
            context={"reason": "patch_file_schema_invalid", "field": "changes"},
        )
    selectors: list[PatchSelector] = []
    allowed = {"node_path", "before_fingerprint", "before_text", "after_text"}
    required = {"node_path", "before_fingerprint", "after_text"}
    for index, item in enumerate(changes):
        if not isinstance(item, dict) or not required.issubset(item) or not set(item).issubset(allowed):
            raise ValidationError(
                "patch change has an invalid selector schema",
                context={"reason": "patch_file_schema_invalid", "change_index": index},
            )
        node_path = item["node_path"]
        before_fingerprint = item["before_fingerprint"]
        before_text = item.get("before_text")
        after_text = item["after_text"]
        if (
            not isinstance(node_path, str)
            or not node_path.startswith("/root[1]/")
            or not isinstance(before_fingerprint, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", before_fingerprint)
            or (before_text is not None and not isinstance(before_text, str))
            or not isinstance(after_text, str)
        ):
            raise ValidationError(
                "patch change fields are invalid",
                context={"reason": "patch_file_schema_invalid", "change_index": index},
            )
        selectors.append(
            PatchSelector(
                node_path=node_path,
                before_fingerprint=before_fingerprint,
                before_text=before_text,
                after_text=after_text,
            )
        )
    return PatchDocument(version=version, changes=tuple(selectors))


def _local_name(qname: str) -> str:
    return qname.rsplit(":", 1)[-1]


def _markup_end(storage: str, start: int) -> int:
    quote: str | None = None
    index = start + 1
    while index < len(storage):
        character = storage[index]
        if quote is not None:
            if character == quote:
                quote = None
        elif character in {'"', "'"}:
            quote = character
        elif character == ">":
            return index + 1
        index += 1
    raise ValidationError("Storage contains an unterminated markup token", context={"reason": "storage_parse_failed"})


def _node_path(stack: list[_Frame], text_ordinal: int) -> str:
    elements = "/".join(f"{frame.local_name}[{frame.ordinal}]" for frame in stack)
    return f"/{elements}/text()[{text_ordinal}]"


def _element_path(stack: list[_Frame]) -> str:
    return "/" + "/".join(f"{frame.local_name}[{frame.ordinal}]" for frame in stack)


def _block_context(stack: list[_Frame]) -> tuple[str, int]:
    """Identify the visible run a leaf under ``stack`` belongs to.

    Returns the canonical path of the nearest ancestor block and that block's
    current run counter. Text outside any block gets its own element path, so
    unrelated top-level leaves never merge into one run.
    """
    for index in range(len(stack) - 1, -1, -1):
        if stack[index].local_name in _BLOCK_ELEMENTS:
            return _element_path(stack[: index + 1]), stack[index].run_index
    return _element_path(stack), stack[-1].run_index


def _break_run(stack: list[_Frame]) -> None:
    """End the visible run of the nearest enclosing block."""
    for index in range(len(stack) - 1, -1, -1):
        if stack[index].local_name in _BLOCK_ELEMENTS:
            stack[index].run_index += 1
            return
    stack[-1].run_index += 1


def _scan_text_segments(storage: str) -> tuple[_TextSegment, ...]:
    stack = [_Frame("root", 1)]
    segments: list[_TextSegment] = []
    cursor = 0
    while cursor < len(storage):
        markup_start = storage.find("<", cursor)
        if markup_start < 0:
            markup_start = len(storage)
        if markup_start > cursor:
            frame = stack[-1]
            frame.text_ordinal += 1
            raw = storage[cursor:markup_start]
            block_path, run_index = _block_context(stack)
            segments.append(
                _TextSegment(
                    start=cursor,
                    end=markup_start,
                    raw=raw,
                    decoded=html.unescape(raw),
                    node_path=_node_path(stack, frame.text_ordinal),
                    eligible=not any(item.local_name in _EXCLUDED_ANCESTORS for item in stack),
                    block_path=block_path,
                    run_index=run_index,
                )
            )
        if markup_start == len(storage):
            break
        if storage.startswith("<!--", markup_start):
            end = storage.find("-->", markup_start + 4)
            if end < 0:
                raise ValidationError(
                    "Storage contains an unterminated comment", context={"reason": "storage_parse_failed"}
                )
            cursor = end + 3
            continue
        if storage.startswith("<![CDATA[", markup_start):
            end = storage.find("]]>", markup_start + 9)
            if end < 0:
                raise ValidationError("Storage contains unterminated CDATA", context={"reason": "storage_parse_failed"})
            # Never patchable, but the text is visible in storage, so record it
            # so a --find that only lives inside a code macro can say so.
            inner = storage[markup_start + 9 : end]
            segments.append(
                _TextSegment(
                    start=markup_start + 9,
                    end=end,
                    raw=inner,
                    decoded=inner,
                    node_path=f"{_element_path(stack)}/cdata()",
                    eligible=False,
                    block_path="",
                    run_index=0,
                    kind="cdata",
                )
            )
            cursor = end + 3
            continue
        if storage.startswith("<?", markup_start):
            end = storage.find("?>", markup_start + 2)
            if end < 0:
                raise ValidationError(
                    "Storage contains an unterminated processing instruction",
                    context={"reason": "storage_parse_failed"},
                )
            cursor = end + 2
            continue
        end = _markup_end(storage, markup_start)
        token = storage[markup_start:end]
        if token.startswith("</"):
            match = _NAME_RE.match(token, 2)
            if match is None or len(stack) == 1 or stack[-1].local_name != _local_name(match.group(0)):
                raise ValidationError(
                    "Storage element boundaries are inconsistent", context={"reason": "storage_parse_failed"}
                )
            stack.pop()
        elif not token.startswith("<!"):
            match = _NAME_RE.match(token, 1)
            if match is None:
                raise ValidationError("Storage start tag is malformed", context={"reason": "storage_parse_failed"})
            qualified_name = match.group(0)
            local_name = _local_name(qualified_name)
            parent = stack[-1]
            ordinal = parent.child_ordinals.get(local_name, 0) + 1
            parent.child_ordinals[local_name] = ordinal
            self_closing = token.rstrip().endswith("/>")

            for attribute in _ATTR_RE.finditer(token):
                value = attribute.group(3) if attribute.group(3) is not None else attribute.group(4)
                if not value:
                    continue
                attribute_path = f"{_element_path(stack)}/{local_name}[{ordinal}]/@{attribute.group(1)}"
                segments.append(
                    _TextSegment(
                        start=markup_start,
                        end=end,
                        raw=value,
                        decoded=html.unescape(value),
                        node_path=attribute_path,
                        eligible=False,
                        block_path="",
                        run_index=0,
                        kind="attribute",
                    )
                )

            # A hard break, a nested block, or any namespaced (macro/resource)
            # element ends the surrounding visible run.
            if local_name in _RUN_BREAK_ELEMENTS or local_name in _BLOCK_ELEMENTS or ":" in qualified_name:
                _break_run(stack)

            if not self_closing:
                stack.append(_Frame(local_name, ordinal))
        cursor = end
    if len(stack) != 1:
        raise ValidationError("Storage contains unclosed elements", context={"reason": "storage_parse_failed"})
    return tuple(segments)


def _validate_fragment(storage: str) -> None:
    parser = ET.XMLParser(resolve_entities=False, recover=False, no_network=True)
    try:
        ET.fromstring(f"{_WRAPPER_PREFIX}{storage}</root>".encode(), parser=parser)
    except ET.XMLSyntaxError as error:
        raise ValidationError(
            "Confluence storage is not a well-formed XML fragment",
            context={"reason": "storage_parse_failed"},
        ) from error


def _version_number(page: Any) -> int:
    value = getattr(page, "version", None)
    if isinstance(value, int):
        return value
    number = getattr(value, "number", None)
    if isinstance(number, int):
        return number
    raise ValidationError("Confluence page version is missing", context={"reason": "version_missing"})


def build_patch_candidate(storage: str, selectors: tuple[PatchSelector, ...]) -> PatchCandidate:
    if not selectors:
        raise ValidationError("patch-text requires at least one selector", context={"reason": "patch_changes_empty"})
    _validate_fragment(storage)
    segments = _scan_text_segments(storage)
    by_path: dict[str, _TextSegment] = {}
    for scanned in segments:
        if not _is_patch_target(scanned):
            # CDATA and attribute segments exist only so a --find can explain
            # itself. They share synthetic paths (two code macros under one
            # parent both yield .../cdata()), so registering them here would let
            # an unrelated macro body make every patch on the page ambiguous.
            continue
        if scanned.node_path in by_path:
            raise ValidationError(
                "Storage text-node identity is ambiguous",
                context={"reason": "storage_node_identity_ambiguous", "node_path": scanned.node_path},
            )
        by_path[scanned.node_path] = scanned

    paths = [selector.node_path for selector in selectors]
    if len(paths) != len(set(paths)):
        raise ValidationError(
            "Patch selectors duplicate or overlap the same text node",
            context={"reason": "duplicate_or_overlapping_selector"},
        )

    replacements: list[tuple[_TextSegment, str]] = []
    applied: list[AppliedPatch] = []
    for selector in selectors:
        segment = by_path.get(selector.node_path)
        if segment is None or not segment.eligible:
            raise ValidationError(
                "Patch selector does not identify an eligible plain-text node",
                context={"reason": "selector_target_unavailable", "node_path": selector.node_path},
            )
        if node_fingerprint(segment.node_path, segment.decoded) != selector.before_fingerprint:
            raise ValidationError(
                "Patch selector fingerprint does not match the current node",
                context={"reason": "selector_fingerprint_mismatch", "node_path": selector.node_path},
            )
        if selector.before_text is not None and segment.decoded != selector.before_text:
            raise ValidationError(
                "Patch selector text does not match the current node",
                context={"reason": "selector_text_mismatch", "node_path": selector.node_path},
            )
        if segment.raw != html.escape(segment.decoded, quote=False):
            raise ValidationError(
                "The selected text node uses an unsupported entity encoding",
                context={"reason": "text_encoding_ambiguous", "node_path": selector.node_path},
            )
        replacements.append((segment, html.escape(selector.after_text, quote=False)))
        applied.append(
            AppliedPatch(
                node_path=selector.node_path,
                before_fingerprint=selector.before_fingerprint,
                before_text=segment.decoded,
                after_text=selector.after_text,
            )
        )

    candidate = storage
    previous_start = len(storage) + 1
    for segment, encoded_after in sorted(replacements, key=lambda item: item[0].start, reverse=True):
        if segment.end > previous_start:
            raise ValidationError(
                "Patch selectors overlap",
                context={"reason": "duplicate_or_overlapping_selector", "node_path": segment.node_path},
            )
        candidate = candidate[: segment.start] + encoded_after + candidate[segment.end :]
        previous_start = segment.start
    _validate_fragment(candidate)
    return PatchCandidate(storage=candidate, changes=tuple(applied))


_HINT_CODES = {
    "text_occurrence_not_unique": "narrow_to_one_occurrence",
    "cross_text_node_boundary": "use_single_plain_text_leaf",
    "unsupported_target_context": "target_is_not_plain_text",
    "text_not_found": "verify_text_against_readable_markdown",
}

# Constant, local templates. Server text is never interpolated into a hint,
# and a hint is never an executable instruction.
_HINT_MESSAGES = {
    "text_occurrence_not_unique": (
        "The text appears in more than one plain-text node. Include enough surrounding "
        "text to identify exactly one, or use pull-md for a structural edit."
    ),
    "cross_text_node_boundary": (
        "The text spans inline markup such as bold, a link or inline code, so no single "
        "plain-text node contains it. Target the text inside one of those parts, or use "
        "pull-md to edit the block as Markdown."
    ),
    "unsupported_target_context": (
        "The text only appears in an attribute, a macro parameter or a macro body, which "
        "patch-text never edits. Use pull-md for that content."
    ),
    "text_not_found": (
        "The text does not appear in any storage node. Read the page with "
        "`page get --body-repr=md` and copy the exact wording."
    ),
}

_NEXT_ACTION_DESCRIPTIONS = {
    "retry_inner_plain_text": "PATCH_RETRY_SINGLE_LEAF",
    "use_pull_md": "PATCH_USE_MANAGED_EDIT",
}

_NEXT_ACTIONS_BY_REASON = {
    "text_occurrence_not_unique": ("retry_inner_plain_text", "use_pull_md"),
    "cross_text_node_boundary": ("retry_inner_plain_text", "use_pull_md"),
    "unsupported_target_context": ("use_pull_md",),
    "text_not_found": ("retry_inner_plain_text",),
}


def _is_patch_target(segment: _TextSegment) -> bool:
    """Only ordinary, eligible character data can ever be replaced."""
    return segment.kind == "text" and segment.eligible


def _exact_spans(haystack: str, needle: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = haystack.find(needle)
    while start >= 0:
        end = start + len(needle)
        spans.append((start, end))
        start = haystack.find(needle, end)
    return spans


def _normalized_spans(haystack: str, needle: str) -> list[tuple[int, int]]:
    """Find ``needle`` by NFC equivalence, returning offsets into the original.

    Offsets are deliberately into the untouched haystack: replacing a span means
    the surrounding characters keep their original normalization form. Rewriting
    the whole leaf as NFC would silently alter text the user never asked to
    change.
    """
    target = unicodedata.normalize("NFC", needle)
    if not target:
        return []
    if unicodedata.is_normalized("NFC", haystack) and unicodedata.is_normalized("NFC", needle):
        # Both sides are already NFC, so equivalence collapses to equality and
        # the quadratic scan below would only repeat what a plain search does.
        return _exact_spans(haystack, needle)
    # A match can only begin where the first character is the target's first
    # character or the first character of its decomposition ("é" composes from
    # "e"), so every other offset can be skipped without normalizing anything.
    openers = {target[0], unicodedata.normalize("NFD", target[0])[0]}

    spans: list[tuple[int, int]] = []
    limit = len(haystack)
    start = 0
    while start < limit:
        if haystack[start] not in openers:
            start += 1
            continue
        matched_end: int | None = None
        end = start + 1
        while end <= limit:
            candidate = unicodedata.normalize("NFC", haystack[start:end])
            if candidate == target:
                matched_end = end
                break
            # NFC never shortens as characters are appended, so once the
            # candidate is longer than the target this start cannot match.
            if len(candidate) > len(target):
                break
            end += 1
        if matched_end is None:
            start += 1
            continue
        spans.append((start, matched_end))
        start = matched_end
    return spans


def _match_spans(haystack: str, needle: str, *, normalized: bool) -> list[tuple[int, int]]:
    return _normalized_spans(haystack, needle) if normalized else _exact_spans(haystack, needle)


def _use_normalized_matching(segments: Sequence[_TextSegment], needle: str) -> bool:
    """Fall back to NFC comparison only when nothing matches exactly.

    Deciding this once for the whole document keeps the result deterministic:
    counts and the selected leaf all come from the same comparison mode, so a
    page never mixes exact and normalized hits.
    """
    return not any(_is_patch_target(segment) and needle in segment.decoded for segment in segments)


def _count_boundary_matches(segments: Sequence[_TextSegment], needle: str, *, normalized: bool = False) -> int:
    """Count occurrences that only exist once adjacent leaves in one run are joined.

    Leaves are grouped by visible run, so text spanning two paragraphs, two
    table cells, two list items or a hard break is never counted: those are not
    adjacent, and reporting them as a boundary match would send the caller
    looking for markup that is not there.
    """
    runs: dict[tuple[str, int], list[_TextSegment]] = {}
    for segment in segments:
        runs.setdefault((segment.block_path, segment.run_index), []).append(segment)

    total = 0
    for run_segments in runs.values():
        if len(run_segments) < 2:
            # A single leaf cannot span a boundary; a match there is an
            # ordinary match and belongs to the eligible count.
            continue
        joined = ""
        spans: list[tuple[int, int]] = []
        for segment in run_segments:
            spans.append((len(joined), len(joined) + len(segment.decoded)))
            joined += segment.decoded

        for position, end in _match_spans(joined, needle, normalized=normalized):
            contained = any(start <= position and end <= stop for start, stop in spans)
            if not contained:
                total += 1
    return total


def _classify_find_failure(segments: Sequence[_TextSegment], needle: str) -> tuple[str, dict[str, Any]]:
    """Resolve why a --find did not select exactly one leaf.

    The precedence is fixed so the answer does not depend on scan order or on
    which candidate happened to be seen first.
    """
    normalized = _use_normalized_matching(segments, needle)
    eligible = [segment for segment in segments if _is_patch_target(segment)]
    match_count = sum(len(_match_spans(segment.decoded, needle, normalized=normalized)) for segment in eligible)
    excluded_match_count = sum(
        len(_match_spans(segment.decoded, needle, normalized=normalized))
        for segment in segments
        if not _is_patch_target(segment)
    )
    boundary_match_count = _count_boundary_matches(eligible, needle, normalized=normalized)

    if match_count >= 2:
        reason = "text_occurrence_not_unique"
    elif boundary_match_count > 0:
        reason = "cross_text_node_boundary"
    elif excluded_match_count > 0:
        reason = "unsupported_target_context"
    else:
        reason = "text_not_found"

    context: dict[str, Any] = {
        "patchable": False,
        "reason": reason,
        "match_count": match_count,
        "boundary_match_count": boundary_match_count,
        "excluded_match_count": excluded_match_count,
        "hint_code": _HINT_CODES[reason],
        "next_actions": [
            {
                "id": action_id,
                "requires_user_approval": False,
                "description_code": _NEXT_ACTION_DESCRIPTIONS[action_id],
            }
            for action_id in _NEXT_ACTIONS_BY_REASON[reason]
        ],
    }
    return reason, context


def _find_failure(segments: Sequence[_TextSegment], needle: str) -> ValidationError:
    reason, context = _classify_find_failure(segments, needle)
    return ValidationError(
        "patch-text must identify exactly one plain-text node occurrence",
        hint=_HINT_MESSAGES[reason],
        context=context,
    )


def _legacy_selector(storage: str, old: str, new: str) -> PatchSelector:
    segments = _scan_text_segments(storage)
    normalized = _use_normalized_matching(segments, old)

    matches: list[tuple[_TextSegment, list[tuple[int, int]]]] = []
    for segment in segments:
        if not _is_patch_target(segment):
            continue
        spans = _match_spans(segment.decoded, old, normalized=normalized)
        if spans:
            matches.append((segment, spans))

    if sum(len(spans) for _segment, spans in matches) != 1:
        raise _find_failure(segments, old)

    segment, spans = matches[0]
    start, end = spans[0]
    # Splice by offset rather than str.replace so that under normalized
    # matching the untouched remainder of the leaf keeps its original bytes.
    after_text = f"{segment.decoded[:start]}{new}{segment.decoded[end:]}"
    return PatchSelector(
        node_path=segment.node_path,
        before_fingerprint=node_fingerprint(segment.node_path, segment.decoded),
        before_text=segment.decoded,
        after_text=after_text,
    )


def _selectors_already_applied(storage: str, selectors: tuple[PatchSelector, ...]) -> bool:
    try:
        segments = _scan_text_segments(storage)
    except ValidationError:
        return False
    by_path = {segment.node_path: segment for segment in segments if _is_patch_target(segment)}
    if len(by_path) != sum(1 for segment in segments if _is_patch_target(segment)):
        return False
    for selector in selectors:
        segment = by_path.get(selector.node_path)
        if segment is None or segment.decoded != selector.after_text:
            return False
        if selector.before_text is not None and (
            node_fingerprint(selector.node_path, selector.before_text) != selector.before_fingerprint
        ):
            return False
    return True


def _legacy_applied_selector(storage: str, old: str, new: str) -> PatchSelector:
    reverse = _legacy_selector(storage, new, old)
    source = build_patch_candidate(storage, (reverse,)).storage
    forward = _legacy_selector(source, old, new)
    candidate = build_patch_candidate(source, (forward,))
    if not _matches_patch_candidate_readback(
        storage,
        candidate_preservation_signature=cfxmark.preservation_signature(candidate.storage),
        changes=_changes_payload(candidate.changes),
    ):
        raise ValidationError(
            "Current page does not prove the requested patch was already applied",
            context={"reason": "patch_retry_not_proven"},
        )
    return forward


def _already_applied_payload(
    page_id: str,
    version: int,
    selectors: tuple[PatchSelector, ...],
    *,
    old: str | None,
    new: str | None,
) -> dict[str, Any]:
    changes = [
        {
            "node_path": selector.node_path,
            "before_fingerprint": selector.before_fingerprint,
            **({"before_text": selector.before_text} if selector.before_text is not None else {}),
            "after_text": selector.after_text,
        }
        for selector in selectors
    ]
    result: dict[str, Any] = {
        "status": "already_applied",
        "patchable": True,
        "match_count": len(selectors),
        "page_id": page_id,
        "version": version,
        "changes": changes,
        "put_count": 0,
        "recovery": "before_after_selector_matched",
    }
    if len(selectors) == 1:
        result.update(
            {
                "node_path": selectors[0].node_path,
                "before": old if old is not None else selectors[0].before_text,
                "after": new if new is not None else selectors[0].after_text,
            }
        )
    return result


def _storage_sha256(storage: str) -> str:
    return hashlib.sha256(storage.encode("utf-8")).hexdigest()


def _changes_payload(changes: tuple[AppliedPatch, ...]) -> list[dict[str, str]]:
    return [
        {
            "node_path": item.node_path,
            "before_fingerprint": item.before_fingerprint,
            "before_text": item.before_text,
            "after_text": item.after_text,
        }
        for item in changes
    ]


def _matches_patch_candidate_readback(
    storage: str,
    *,
    candidate_preservation_signature: str,
    changes: Any,
) -> bool:
    """Prove a server-normalized read-back contains exactly the requested leaf edits."""

    try:
        observed_signature = cfxmark.preservation_signature(storage)
    except cfxmark.CfxmarkError:
        return False
    if observed_signature != candidate_preservation_signature:
        return False
    if not isinstance(changes, list) or not changes:
        return False
    expected_after_by_path: dict[str, str] = {}
    for change in changes:
        if not isinstance(change, dict):
            return False
        node_path = change.get("node_path")
        after_text = change.get("after_text")
        if not isinstance(node_path, str) or not isinstance(after_text, str) or node_path in expected_after_by_path:
            return False
        expected_after_by_path[node_path] = after_text
    try:
        segments = {segment.node_path: segment for segment in _scan_text_segments(storage)}
    except ValidationError:
        return False
    return all(
        (segment := segments.get(node_path)) is not None and segment.eligible and segment.decoded == after_text
        for node_path, after_text in expected_after_by_path.items()
    )


def patch_text(
    client: PatchTextClient,
    page_id: str,
    *,
    old: str | None = None,
    new: str | None = None,
    patch_document: PatchDocument | None = None,
    if_version: int | None = None,
    dry_run: bool = False,
    reason: str | None = None,
    minor_edit: bool = False,
) -> dict[str, Any]:
    """Apply one state-free exact-leaf patch with stale and response-loss recovery."""

    if patch_document is None:
        if not old or new is None or old == new:
            raise ValidationError("patch-text requires distinct non-empty old and new text")
    elif old is not None or new is not None:
        raise ValidationError(
            "Use either --patch-file or --find/--replace, not both",
            context={"reason": "patch_input_conflict"},
        )

    page = client.get_page(page_id)
    storage = getattr(page, "body_storage", None)
    if not isinstance(storage, str):
        raise ValidationError("Confluence storage body is missing", context={"reason": "storage_missing"})
    current_version = _version_number(page)
    expected_version = patch_document.version if patch_document is not None else if_version
    if expected_version is None:
        expected_version = current_version
    if current_version == expected_version + 1:
        applied_selectors: tuple[PatchSelector, ...]
        if patch_document is None:
            assert old is not None and new is not None
            try:
                applied_selectors = (_legacy_applied_selector(storage, old, new),)
            except ValidationError:
                applied_selectors = ()
        else:
            applied_selectors = patch_document.changes
        if applied_selectors and _selectors_already_applied(storage, applied_selectors):
            return _already_applied_payload(
                page_id,
                current_version,
                applied_selectors,
                old=old,
                new=new,
            )
    if current_version != expected_version:
        raise StaleError(
            "Confluence page version changed",
            context={
                "reason": "patch_version_mismatch",
                "expected": expected_version,
                "actual": current_version,
                "page_id": page_id,
            },
        )
    selectors: tuple[PatchSelector, ...]
    if patch_document is None:
        assert old is not None and new is not None
        selectors = (_legacy_selector(storage, old, new),)
    else:
        selectors = patch_document.changes
    planned = build_patch_candidate(storage, selectors)
    candidate = planned.storage
    changes = _changes_payload(planned.changes)
    result: dict[str, Any] = {
        "status": "dry_run" if dry_run else "updated",
        "patchable": True,
        "match_count": len(changes),
        "page_id": page_id,
        "version": current_version if dry_run else current_version + 1,
        "changes": changes,
        "put_count": 0 if dry_run else 1,
    }
    if len(changes) == 1:
        result.update(
            {
                "node_path": changes[0]["node_path"],
                "before": old if old is not None else changes[0]["before_text"],
                "after": new if new is not None else changes[0]["after_text"],
            }
        )
    if dry_run:
        return result

    prewrite = client.get_page(page_id)
    prewrite_storage = getattr(prewrite, "body_storage", None)
    if (
        not isinstance(prewrite_storage, str)
        or _version_number(prewrite) != current_version
        or _storage_sha256(prewrite_storage) != _storage_sha256(storage)
    ):
        raise StaleError(
            "Confluence page changed immediately before patch PUT",
            context={"reason": "prewrite_remote_drift", "page_id": page_id},
        )
    prewrite_title = getattr(prewrite, "title", None)
    if not isinstance(prewrite_title, str):
        raise ValidationError("Confluence page title is missing", context={"reason": "title_missing"})
    update_error: Exception | None = None
    try:
        client.update_page(
            page_id,
            prewrite_title,
            candidate,
            current_version + 1,
            body_format="storage",
            reason=reason,
            minor_edit=minor_edit,
        )
    except Exception as error:
        update_error = error
    observed = client.get_page(page_id)
    observed_storage = getattr(observed, "body_storage", None)
    candidate_observed = (
        isinstance(observed_storage, str)
        and _version_number(observed) == current_version + 1
        and _matches_patch_candidate_readback(
            observed_storage,
            candidate_preservation_signature=cfxmark.preservation_signature(candidate),
            changes=changes,
        )
    )
    if candidate_observed:
        if update_error is not None:
            result["recovery"] = "lost_response_adopted"
        return result
    source_observed = (
        isinstance(observed_storage, str)
        and _version_number(observed) == current_version
        and _storage_sha256(observed_storage) == _storage_sha256(storage)
    )
    if update_error is not None and source_observed:
        raise ValidationError(
            "patch-text PUT did not change the remote page",
            context={"page_id": page_id, "reason": "patch_put_failed"},
        ) from update_error
    if update_error is not None:
        raise ConflictError(
            "patch-text PUT outcome is ambiguous",
            context={"page_id": page_id, "reason": "patch_put_outcome_ambiguous"},
        ) from update_error
    raise ValidationError(
        "patch-text server read-back did not match the requested leaf change",
        context={"page_id": page_id, "reason": "readback_mismatch"},
    )


__all__ = [
    "AppliedPatch",
    "PatchCandidate",
    "PatchDocument",
    "PatchSelector",
    "build_patch_candidate",
    "node_fingerprint",
    "parse_patch_document",
    "patch_text",
]
