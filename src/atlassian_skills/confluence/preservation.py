"""Which unclassifiable structures the managed publish path is proven to preserve.

§8.2.1. `xhtml_required` used to mean two things at once, and mixing them cost 49% of the
live corpus its Markdown workflow:

* **Markdown expressiveness** -- regenerating this page from Markdown alone would lose a
  colspan, a cell colour, a nested table. That is true and has to be *explained*.
* **Managed preservation** -- the candidate built from the current remote keeps that
  structure, and the pre-publish proof confirms it. That is a different question, and
  where it is true the answer has to be *not to block*.

The boundary, stated once because everything here follows from it:

    raw Markdown's loss must be explained. The actual candidate's loss must be prevented.

So a capability is a claim about the second axis, and it is deliberately hard to make:

* it names the exact diagnostic codes it covers, so a structure nobody has considered is
  not swept in by resembling one that was;
* it is closed by a contract test that publishes through the *public push path*. A
  capability whose proof runs against an internal helper proves nothing about what a user
  will do. `test_the_registry_capabilities_are_closed_by_a_real_contract_test` asserts each
  named test exists and goes through `push_md`;
* it refuses to cover a page whose unknown findings touch **content**. Topology and
  presentation can be spliced back; text cannot be assumed.

Promotion is one structure at a time. `colspan`/`rowspan` on a flat table is the first
positive case; a table inside a table cell, a table inside an expand, or a table inside a
macro are not included and do not become included by sharing codes with it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

import cfxmark


@dataclass(frozen=True)
class PreservationCapability:
    """One proven claim: these codes, on this shape, survive the managed publish."""

    #: Stable id, reported to the caller and quoted in the manifest.
    name: str
    #: Every diagnostic code this capability accounts for. A page carrying any unknown
    #: code outside this set is not covered.
    codes: frozenset[str]
    #: What the author must be told is protected remotely and not editable in Markdown.
    protects: tuple[str, ...]
    #: The contract test that closes this capability, through the public push path.
    closed_by: str
    #: A shape discriminator. Codes alone are insufficient: a ragged table and a
    #: nested or presentation-bearing table can report the same element/topology
    #: differences while requiring different edit policies. New capabilities must
    #: opt in to a deliberately implemented shape check; an unspecified shape
    #: cannot grant Markdown write permission.
    shape: str = "unregistered"
    #: Exact converter/profile pairs on which the public-path contract was closed.
    #: An empty set is useful for a defined-but-unregistered candidate, never for a
    #: registered capability.
    builds: frozenset[tuple[str, str]] = frozenset()

    def covers(
        self,
        findings: list[dict[str, Any]],
        storage: str | None,
        *,
        converter: str | None,
        profile: str | None,
        base_artifact: Any | None = None,
    ) -> bool:
        if self.builds and (converter, profile) not in self.builds:
            return False
        if self.shape == "ragged-protected-tables":
            return storage is not None and _ragged_protected_tables_cover(
                findings,
                storage,
                base_artifact=base_artifact,
            )
        if self.shape == "flat-merged-tables":
            return storage is not None and _flat_merged_tables_cover(storage)
        # Fail closed for an unrecognised shape. `preservation_for` already checks
        # the diagnostic-code set, but that is deliberately not enough evidence to
        # say that an arbitrary page carries a structure this capability can keep.
        return False


#: Codes that describe *text*. Never covered by any capability: a claim that content
#: survives cannot rest on a splice, and this is the line between explaining a loss and
#: preventing one.
_CONTENT_CODE = re.compile(r"#text$|#content$")

#: A path segment for a table. Two of them in one path means a table inside a table,
#: which no capability here covers.
_TABLE_SEGMENT = re.compile(r"(?:^|/)table\[")


TABLE_SPLICE_V1 = PreservationCapability(
    name="table-splice-v1",
    codes=frozenset(
        {
            # Header and row topology, which a Markdown table cannot express and the
            # splice carries over untouched.
            "th#element",
            "th#topology",
            "tr#element",
            "tr#topology",
            "thead#element",
            "thead#topology",
            "tbody#element",
            "tbody#topology",
            "table#topology",
            "td#element",
            "td#topology",
            # The two span attributes this capability is named for.
            "td@colspan",
            "td@rowspan",
            "th@colspan",
            "th@rowspan",
        }
    ),
    protects=("merged table cells (colspan/rowspan) and the header/row structure around them",),
    closed_by="test_a_flat_table_with_merged_cells_survives_a_managed_publish",
    shape="flat-merged-tables",
)

RAGGED_TABLE_ISLAND_V1 = PreservationCapability(
    name="ragged-table-island-v1",
    codes=frozenset(
        {
            "th#element",
            "th#topology",
            "tr#element",
            "tr#topology",
            "thead#element",
            "thead#topology",
            "tbody#element",
            "tbody#topology",
            "table#topology",
            "td#element",
            "td#topology",
        }
    ),
    protects=("ragged table row/cell topology; edit Markdown outside the protected table only",),
    closed_by="test_a_ragged_table_island_survives_the_public_managed_push",
    shape="ragged-protected-tables",
    builds=frozenset({("cfxmark 0.6.0", "editable")}),
)

#: The registry contains only the ragged-table shape closed by the public managed
#: pull/push contract. It does not promote nested tables, colours, or arbitrary pages
#: that happen to report the same topology codes.
#:
#: It *does* promote a flat merged-cell table, which this comment used to deny. A
#: `colspan` row and a genuinely ragged row are indistinguishable in the Markdown
#: projection -- both leave `ragged_protected_table_paths` a row-width set larger than
#: one -- so the shape is admitted. Measured before deciding which half was wrong: the
#: island is carried through byte for byte with `colspan` intact and an edit inside it is
#: still refused, so the capability keeps its promise on that shape and the scope
#: sentence was the error. Closed by
#: `test_a_merged_cell_table_that_projects_as_ragged_is_covered_and_preserved` rather
#: than left admitted by accident.
#:
#: `TABLE_SPLICE_V1` is defined above and not registered, because its contract test
#: refused to close it. Measured on the flat merged-cell page, publishing an edit to the
#: prose beside the table:
#:
#:     path          no edit                    prose edit
#:     managed       no_change,  PUT 0          REFUSED ownership_proof_invalid, PUT 0
#:     non-managed   no_change,  PUT 0          updated, colspan kept,           PUT 1
#:
#: So the structure does survive a publish -- on the path that does not carry a manifest.
#: The managed path refuses the edit outright, and it is the managed Markdown write that a
#: capability exists to unlock. Registering it would have unlocked a file whose edits
#: cannot be published through the workflow the file belongs to, which is the half work
#: product §8.2 forbids, reached by a longer route.
#:
#: This is worth being precise about because it corrects an earlier claim of mine. The U4
#: evidence argued that §8.2's `xhtml_required` row should be amended because "the managed
#: publish path splices the unclassified subtree back in untouched", citing
#: `test_a_page_classified_unknown_still_publishes_when_nothing_is_lost`. That test
#: publishes with `managed=False`. The observation was real and the attribution was wrong,
#: and what actually stands between these pages and a working Markdown workflow is the
#: ownership proof rather than §8.2.
#:
#: With this single entry, only a page whose unknown findings are wholly inside
#: cfxmark's authenticated ragged-table protected regions may write a canonical
#: Markdown file. Everything else keeps §8.2.1's conservative XHTML branch.
CAPABILITIES: tuple[PreservationCapability, ...] = (RAGGED_TABLE_ISLAND_V1,)


@dataclass(frozen=True)
class IdentityPreservation:
    """§8.2's precondition for `markdown_identity_bound`, as a registry entry.

    The grade means "Markdown holds the content, and publishing carries the identity
    forward". §8.2 permits it only where that carry is *statically registered* for a
    converter and profile and closed by a contract test on the public push path -- not
    where the code merely believes it works. The row said `False` with a comment
    deferring the gate, and while that comment stood, 14 of 55 live pages had been
    written on the belief.

    Every field is a thing that must be true for the permission to hold:

        converter   the carry is cfxmark's behaviour, so the evidence is about a version
                    of cfxmark. Checked at runtime, because a converter upgrade changes
                    exactly the mechanism this depends on and a detached macro renders
                    identically -- nothing would look wrong.
        profile     the profiles disagree about macros. A macro in a table cell survives
                    the round trip under the default profile and loses its body under
                    `editable`, which is the profile every atls path uses.
        shapes      the page shapes this carry is proven for *and permits*.
        refused_shapes
                    shapes the tests exercise and the carry deliberately declines. Kept
                    apart from `shapes` because one list holding both reads as a list of
                    permissions: two indistinguishable macros and the `ri:*` attachment
                    fields both sat in `shapes` while `covers()` refuses the first and
                    `covers_attributes` omits the second, so the entry advertised a reach
                    it does not have. Scope of evidence and scope of permission are
                    different questions and a reader needs both.
        closed_by   the test on the public push path. §8.2 asks for the contract test by
                    name because a capability whose proof lives beside the mechanism it
                    proves is not a check.
    """

    name: str
    converter: str
    profile: str
    shapes: tuple[str, ...]
    closed_by: str
    #: Measured, and refused anyway. Empty is a claim too -- it says nothing was tried
    #: and declined -- so it is spelled out rather than defaulted away.
    refused_shapes: tuple[str, ...] = ()
    #: The identity-bearing `(element, attribute)` pairs a carry was actually proven for.
    #:
    #: R4-pre P1: `shapes` is prose and the lookup checked only the converter and profile, so
    #: every identity-bound page on this build was granted a canonical write -- including
    #: identity structures nobody had measured. That is §8.2's own defect one level in: a name
    #: promising a check, with the check scoped to the wrong thing.
    covers_attributes: frozenset[tuple[str, str]] = frozenset()

    def covers(self, findings: list[dict[str, Any]], storage: str) -> bool:
        """Whether every identity this page carries is one the carry was proven for.

        `storage` is required, and deliberately so. It defaulted to `""`, and the default
        failed open: `_has_indistinguishable_macros("")` answers `False` -- "no page was
        offered, so nothing was found" -- which reads here as "no ambiguity", and a caller
        who forgot the argument was granted the carry with the ambiguity check never run.
        A missing argument now raises instead of quietly approving.

        Two refusals beyond the attribute list, because an attribute being proven is not the
        same as this page's use of it being decidable:

        * **two macros the carry cannot tell apart.** Byte-identical macros with different
          ids both land in `identity_carry`, and `IDENTITY_CARRY`'s own docstring says nothing
          in a positional walk can say which id belongs to which.

          Asked of the *content signatures*, not of a count. Counting was the first attempt
          and it contradicted the measurement this registry rests on: a measured live page carries
          two macros and both ids survived an ordinary edit, because they have different
          bodies and are matched by content. Refusing every multi-macro page would have
          declined 11 of the 24 adoptable documents on a rule the live evidence disproves.
        * **any unknown finding.** An unknown means something on the page was not classified
          at all, and a carry proven against classified structures says nothing about it.
        """

        identity = [row for row in findings if row.get("verdict") == "identity_carry"]
        if not identity:
            return True
        if any(row.get("verdict") == "unknown" for row in findings):
            return False
        for row in identity:
            code = str(row.get("code") or "")
            element, _, attribute = code.partition("@")
            if not attribute or (element, attribute) not in self.covers_attributes:
                return False
        return not _has_indistinguishable_macros(storage)


#: The carry, registered. What it rests on, in the order the evidence arrived:
#:
#:   * the pre-write gate refuses any candidate that would drop or rebind an id, both
#:     ways of losing one, and a refused publish sends no write at all. 25 tests.
#:   * the post-write census compares the ids we sent against the ids the server stored,
#:     by value *and* count, and a mismatch gets its own receipt word rather than
#:     `reconciled`. This is the half that did not exist before A5: the readback compares
#:     the two sides as Markdown, and Markdown does not carry `ac:macro-id`.
#:   * live, 2026-07-29: both atls paths carry the base forward and a macro keeps its id
#:     through an edit inside its own body.
#:
#: Not registered on "the code looks right". Registered on a gate that refuses, a census
#: that reports, and a live measurement -- and bound to the converter version so that
#: none of it is inherited by a build none of it was measured against.
IDENTITY_PRESERVATION: IdentityPreservation | None = IdentityPreservation(
    name="identity-carry-v1",
    converter="cfxmark 0.6.0",
    profile="editable",
    shapes=(
        "a structured macro with a rich-text body",
        "a structured macro with parameters and no body",
        "several macros on one page, each with a distinct content signature",
    ),
    #: Exercised and declined. Both used to sit in `shapes`, where they read as reach.
    refused_shapes=(
        "two macros on one page the carry cannot tell apart -- `covers()` refuses the page",
        "an attachment reference (ri:filename, ri:version-at-save) -- outside `covers_attributes`",
    ),
    closed_by="tests/unit/test_identity_gate.py::test_the_managed_path_publishes_and_keeps_the_identity",
    #: Only `ac:macro-id`. That is what the live measurement was about -- one live page, two
    #: macros, an ordinary prose edit through `push-md`, both ids unchanged. `ac:name` and
    #: `ac:local-id` travel in the same registry entry in `IDENTITY_BEARING` and were not
    #: measured, and neither were the `ri:*` attachment fields, so none of them is here.
    covers_attributes=frozenset({("ac:structured-macro", "ac:macro-id")}),
)


def identity_preservation_for(converter: str, profile: str) -> IdentityPreservation | None:
    """The registered carry, or `None` when this build is not the one it was proven on.

    `None` is the safe answer: the grade falls back to refusing the canonical write,
    which is where it was before the registry existed.
    """

    registered = IDENTITY_PRESERVATION
    if registered is None:
        return None
    if registered.converter != converter or registered.profile != profile:
        return None
    return registered


def _has_indistinguishable_macros(storage: str) -> bool:
    """Whether two macros on this page share a content signature.

    That is the shape a positional carry cannot resolve, and it is asked of the same grouping
    the identity gate itself uses, so the two cannot disagree about what "the same macro"
    means. An empty `storage` answers `False`: the caller has not offered the page, and the
    attribute check above has already done the work it can.
    """

    if not storage:
        return False
    from atlassian_skills.confluence.identity_gate import MACRO_ELEMENT, TRACKED, _instances

    signatures = [signature for signature, _values in _instances(storage, MACRO_ELEMENT, TRACKED)]
    return len(signatures) != len(set(signatures))


def unknown_codes(findings: list[dict[str, Any]]) -> tuple[str, ...]:
    """The codes that made this page unclassifiable, in order."""

    return tuple(str(row.get("code")) for row in findings if row.get("verdict") == "unknown")


def preservation_for(
    findings: list[dict[str, Any]],
    storage: str | None = None,
    *,
    converter: str | None = None,
    profile: str | None = None,
    base_artifact: Any | None = None,
) -> PreservationCapability | None:
    """The capability covering this page, or `None` -- which means the XHTML workflow.

    `None` is the safe answer and the default one. A page qualifies only if every
    unknown finding is inside one capability's code set, no unknown finding describes
    content, and nothing sits inside another table.
    """

    codes = unknown_codes(findings)
    if not codes:
        return None
    if any(_CONTENT_CODE.search(code) for code in codes):
        return None
    if _nests_a_table(findings):
        return None
    for capability in CAPABILITIES:
        if set(codes) <= capability.codes and capability.covers(
            findings,
            storage,
            converter=converter,
            profile=profile,
            base_artifact=base_artifact,
        ):
            return capability
    return None


def _ragged_protected_tables_cover(
    findings: list[dict[str, Any]],
    storage: str,
    *,
    base_artifact: Any | None = None,
) -> bool:
    """Whether every unknown lies inside an authenticated ragged table island."""

    artifact = base_artifact
    if artifact is None:
        try:
            artifact = cfxmark.to_md_artifact(
                storage,
                options=cfxmark.ConversionOptions(profile="editable"),
            )
        except (ValueError, TypeError):
            return False

    ragged_paths = ragged_protected_table_paths(artifact)
    if not ragged_paths:
        return False

    unknown_paths: list[tuple[str, ...]] = []
    for row in findings:
        if row.get("verdict") != "unknown":
            continue
        paths = row.get("semantic_paths") or ()
        if not paths:
            return False
        unknown_paths.extend(tuple(str(path).split("/")) for path in paths)
    return bool(unknown_paths) and all(
        any(path[: len(ragged)] == ragged for ragged in ragged_paths) for path in unknown_paths
    )


def _flat_merged_tables_cover(storage: str) -> bool:
    """Whether every table is flat and at least one carries a supported span.

    `TABLE_SPLICE_V1` is not registered today, but its discriminator must still be
    concrete.  A future registration must not turn its diagnostic-code allowlist
    into permission for a nested table or one embedded in a Confluence macro.
    """

    try:
        root = ElementTree.fromstring(f'<root xmlns:ac="http://atlassian.com/content">{storage}</root>')
    except ElementTree.ParseError:
        return False

    parents = {child: parent for parent in root.iter() for child in parent}
    tables = [element for element in root.iter() if _local_name(element.tag) == "table"]
    if not tables:
        return False

    has_span = False
    for table in tables:
        if any(_local_name(descendant.tag) == "table" for descendant in table.iter() if descendant is not table):
            return False
        if any(_local_name(ancestor.tag) == "structured-macro" for ancestor in _ancestors(table, parents)):
            return False
        has_span |= any(
            _local_name(attribute) in {"colspan", "rowspan"}
            for cell in table.iter()
            if _local_name(cell.tag) in {"td", "th"}
            for attribute in cell.attrib
        )
    return has_span


def _ancestors(
    element: ElementTree.Element[str], parents: dict[ElementTree.Element[str], ElementTree.Element[str]]
) -> tuple[ElementTree.Element[str], ...]:
    """Return ancestors from parent to root without exposing ElementTree internals."""

    result: list[ElementTree.Element[str]] = []
    current = parents.get(element)
    while current is not None:
        result.append(current)
        current = parents.get(current)
    return tuple(result)


def _local_name(value: str) -> str:
    """The namespace-free XML name used by the narrow table shape checks."""

    return value.rsplit("}", 1)[-1]


def ragged_protected_table_paths(artifact: Any) -> frozenset[tuple[str, ...]]:
    """The exact remote paths protected because row widths are ragged."""

    protected_paths = {
        tuple(region.remote_node_path)
        for region in artifact.protected_regions
        if region.kind == "table-island" and region.edit_policy == "preserve_only"
    }
    ragged_paths: set[tuple[str, ...]] = set()
    for block in artifact.document.children:
        source_path = tuple(getattr(block, "source_path", ()) or ())
        body = getattr(block, "body", None)
        if not source_path or body is None or source_path not in protected_paths:
            continue
        header = getattr(block, "header", None)
        rows = (*((header,) if header is not None else ()), *body)
        if len({len(getattr(row, "cells", ())) for row in rows}) > 1:
            ragged_paths.add(source_path)
    return frozenset(ragged_paths)


def _nests_a_table(findings: list[dict[str, Any]]) -> bool:
    """Whether any finding sits inside a table that is itself inside a table.

    Defence in depth, and measured to be exactly that. The premise is real -- a nested
    table produces almost the same code set as a flat one with merged cells, so a code
    set alone would promote it, and the paths are the only thing that tells them apart.
    But on the storage shapes reachable today it is not the clause that refuses: every
    nested table measured, down to one whose cells are empty, also emits `td#text`, so
    `_CONTENT_CODE` returns one line above and this never runs.

    Left in rather than deleted, because what makes it unreachable is a fact about the
    converter's output and not about this rule. `test_nested_tables_are_refused` asserts
    both halves, so the day a nested table arrives without a content code the guard is
    already here and the test says which clause caught it.
    """

    for row in findings:
        for path in row.get("semantic_paths") or ():
            if len(_TABLE_SEGMENT.findall(str(path))) > 1:
                return True
    return False


__all__ = [
    "CAPABILITIES",
    "RAGGED_TABLE_ISLAND_V1",
    "TABLE_SPLICE_V1",
    "PreservationCapability",
    "preservation_for",
    "ragged_protected_table_paths",
    "unknown_codes",
]
