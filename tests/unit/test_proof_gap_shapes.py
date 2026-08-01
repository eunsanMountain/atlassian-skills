"""The live corpus's proof failures, reduced to fixtures that reproduce them here.

Four of the thirteen blocked Story documents refuse with `unclassified-storage-change`,
and until 2026-07-31 nobody could say why: the diagnosis lane read a capped leaf list
through a key the error never carried, so its "shared root causes" were groups of ten
samples keyed on a node name that was always `None`.

Measured signature, from one archive-wheel census run over the live corpus, recorded
outside this repository with per-document remote and staged hashes:

    page A   133 leaves   strong element/tail/text/topology, li topology, p element/topology
    page B    33 leaves   li text/topology, p element/text/topology, strong element/tail/text
    page C    22 leaves   strong element/tail/text/topology, li topology, p element/topology
    page D     6 leaves   li topology

Everything here goes through `build_managed_preflight`, never through a hand-rolled
`to_cfx_artifact(base_artifact=..., splice_source=...)`. That shortcut is measurably a
different question -- on one of them it reports 157 unclassified leaves where the preflight
reports 6 -- and the first version of this file used it and produced a "reproduction" the
product does not actually make. The ledger had written that trap down. It caught me anyway.
"""

from __future__ import annotations

import collections
from pathlib import Path

import cfxmark

from atlassian_skills.confluence.migration_preflight import build_managed_preflight
from atlassian_skills.core.errors import AtlasError
from tests.unit.managed_seam import pull_managed_suspending_the_write_policy
from tests.unit.test_managed_error_redaction import ManagedClient

EDITABLE = cfxmark.ConversionOptions(profile="editable")

# The page as published: a paragraph, a list whose items are paragraph-wrapped and carry
# inline emphasis, a closing paragraph. Deliberately ordinary.
BODY = (
    "<p>intro line here</p>"
    "<ul><li><p>alpha <strong>bold</strong> item</p></li><li><p>bravo item</p></li></ul>"
    "<p>outro line here</p>"
)
# The same page carrying one thing Markdown cannot hold. Every Story document in that corpus has
# something in this role; most of them have an HTML comment.
NOT_LOSSLESS = BODY + "<table><tbody><tr><td>cell</td></tr></tbody></table>"

# An authored document that kept being edited after its page was published: prose revised,
# two list items added, a section appended.
DRIFTED = (
    "intro line here, revised substantially by the author\n"
    "\n"
    "- alpha **bold** item, with a much longer trailing clause\n"
    "- bravo item\n"
    "- charlie item the page never had\n"
    "- delta item also new\n"
    "\n"
    "outro line here\n"
    "\n"
    "## A section the page does not have\n"
    "\n"
    "Prose under it.\n"
)


def _publish(source: str, edited_markdown: str, tmp_path: Path) -> tuple[dict, collections.Counter]:
    """Adopt the page, graft the authored body under its manifest, and preflight it.

    Returns `(outcome, shapes)`. `outcome` is the error context when the proof refuses and
    the ownership payload when it does not, so a caller can tell "passed" from "refused
    with nothing to report" -- which is the distinction this whole area kept collapsing.

    The manifest line is kept and the body grafted beneath it, the same way
    `harness/proof_gaps.py` adopts a corpus document. Overwriting the whole file throws the
    manifest away and the preflight refuses with an invalid manifest long before any proof.
    """

    client = ManagedClient(source)
    managed = tmp_path / f"page-{abs(hash((source, edited_markdown))):x}.md"
    pull_managed_suspending_the_write_policy(client, "123", managed, no_assets=True)
    manifest = managed.read_text(encoding="utf-8").split("\n", 1)[0]
    managed.write_text(manifest + "\n" + edited_markdown, encoding="utf-8")

    shapes: collections.Counter = collections.Counter()
    try:
        preflight = build_managed_preflight(client, "123", managed)
    except AtlasError as refused:
        context = refused.context or {}
        for identity in context.get("all_identities") or []:
            path = identity.get("path") or []
            shapes[(str(path[-1]).split("[")[0] if path else None, identity.get("field"))] += 1
        assert client.puts == 0
        return context, shapes
    assert client.puts == 0
    return dict(preflight.ownership), shapes


def _round_trip(source: str) -> str:
    return cfxmark.to_md_artifact(source, options=EDITABLE).markdown


def test_a_lossless_page_waives_the_proof_however_far_its_document_drifted(tmp_path: Path) -> None:
    """Not a control -- a finding, and the one that makes the rest of this file mean
    something.

    A page Markdown can hold completely has nothing bound to its source, so the ownership
    question is moot and the proof is waived no matter what the local document says. Drift
    alone therefore cannot be why the four corpus documents refuse: on a lossless page the
    same drift produces no refusal at all.
    """

    outcome, shapes = _publish(BODY, DRIFTED, tmp_path)

    assert shapes == {}
    assert outcome["proof_waived"] is True
    assert outcome["proof_mootness"]["moot"] is True
    assert outcome["proof_mootness"]["reason"] == "nothing_bound_to_the_source"


def test_one_thing_markdown_cannot_hold_turns_the_same_drift_into_the_corpus_signature(
    tmp_path: Path,
) -> None:
    """The reproduction, and it needs both halves.

    Add a table to the page -- one structure Markdown cannot round-trip -- and the identical
    authored document now refuses with `unclassified-storage-change` and exactly the node
    and field shapes the four corpus documents show.

    So the mechanism is two-part: something must make the page non-lossless before the proof
    runs at all, and then the document's drift is what it cannot attribute. Neither alone
    does it. That is why `html-comment-dropped` appears on all six adopted documents while
    explaining none of them -- it is what opens the gate, not what fails behind it.

    **Still not established**: that these documents fail for this reason. A matching
    signature says where to look. Confirming it is a per-document check against the live
    page and has not been done for any of the thirteen.
    """

    outcome, shapes = _publish(NOT_LOSSLESS, DRIFTED, tmp_path)

    assert outcome["reason"] == "ownership_proof_invalid"
    assert outcome["fatal_class"] == "unclassified-storage-change"
    # Asserted as a set: the counts scale with how far a given document drifted, and pinning
    # them would make this a test about the fixture's size.
    assert {node for node, _field in shapes} == {"li", "p", "strong"}
    for shape in (("li", "topology"), ("p", "element"), ("strong", "element"), ("strong", "tail")):
        assert shape in shapes


def test_the_non_lossless_page_still_takes_an_ordinary_edit(tmp_path: Path) -> None:
    """The other half of the pair. A table on the page is not itself disqualifying -- an
    ordinary edit to the same page proves cleanly and publishes.

    Without this, the test above would equally describe "any page with a table refuses",
    and the remedy would go looking for a table defect that is not there.
    """

    edited = _round_trip(NOT_LOSSLESS).replace("intro line", "INTRO line")

    outcome, shapes = _publish(NOT_LOSSLESS, edited, tmp_path)

    assert shapes == {}
    assert "reason" not in outcome


def test_one_added_list_item_is_not_enough_drift(tmp_path: Path) -> None:
    """Where the boundary is *not*, on the page that can refuse.

    "Partial overlap defeats the source map" was the obvious explanation and this refuses
    it: a single inserted item attributes cleanly even here. Which combination of concurrent
    changes turns a decidable match into an ambiguous one is open, and the four documents'
    remedy depends on the answer, so it should not be guessed at.
    """

    one_more = _round_trip(NOT_LOSSLESS).replace("bravo item", "bravo item\n- charlie item the page never had")

    _outcome, shapes = _publish(NOT_LOSSLESS, one_more, tmp_path)

    assert shapes == {}
