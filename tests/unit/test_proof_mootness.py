"""When attribution cannot matter, and -- measured -- how rarely that is.

The ownership proof refuses edits it cannot attribute to a remote leaf, and on
one corpus of twenty pages it refused five that dropped nothing at all. The
narrowing here is not to trust it less: it is to check, on the document about to
be written, whether the question it asks can change anything.

The result of building it is worth stating plainly, because it is not the result
that was expected. On this corpus the gate opens on nothing. Every measured
over-block is a page whose publish really would alter content the author never
touched -- so the refusal was right, and the over-blocking lives upstream, in a
converter gap that was deliberately deferred.

A gate that never opens looks like dead code. This one is the difference between
"we checked and it is not safe" and "we assumed it was not safe", and the last
test in the first section is the evidence for that.
"""

from __future__ import annotations

import cfxmark
import pytest

from atlassian_skills.confluence.proof_mootness import assess_proof_mootness

OPTIONS = cfxmark.ConversionOptions(profile="markdown-first")

PLAIN = "<p>alpha paragraph text here</p><p>bravo paragraph text here</p>"
# Mixed on purpose. A live corpus page exposed the combination of one wrapped item, one bare
# item and one multi-paragraph item; the per-item marker now gives that exact
# storage shape a Markdown spelling.
WITH_LIST = (
    "<p>alpha paragraph text here</p>"
    "<ul><li><p>one item</p></li><li>two item</li><li><p>three item</p><p>and more</p></li></ul>"
    "<p>bravo paragraph text here</p>"
)
WITH_MACRO = (
    "<p>alpha paragraph text here</p>"
    '<ac:structured-macro ac:name="info" ac:macro-id="abc-123">'
    "<ac:rich-text-body><p>notice text</p></ac:rich-text-body></ac:structured-macro>"
)
CELL_BACKGROUND = (
    "<p>alpha paragraph text here</p>"
    "<table><thead><tr><th>h</th></tr></thead>"
    '<tbody><tr><td data-highlight-colour="#ff0000">a</td></tr></tbody></table>'
)


def _round_trip(storage: str) -> str:
    return cfxmark.to_md_artifact(storage, options=cfxmark.ConversionOptions(profile="editable")).markdown


def _candidate(storage: str, markdown: str) -> str:
    return cfxmark.to_cfx_artifact(markdown, splice_source=storage, options=OPTIONS).xhtml


def _assess(storage: str, markdown: str):
    return assess_proof_mootness(storage, _candidate(storage, markdown), markdown, options=OPTIONS)


# --------------------------------------------------------------------------
# What holds the gate closed, and why each one has to
# --------------------------------------------------------------------------


def test_a_page_markdown_holds_entirely_has_nothing_for_attribution_to_get_wrong() -> None:
    """The one case the narrowing is for. Nothing in the remote needs carrying
    over, and the candidate is what a plain render of the edited Markdown
    produces, so the source map's belief about it changed nothing."""

    markdown = _round_trip(PLAIN).replace("alpha paragraph", "alpha edited")
    assert _assess(PLAIN, markdown).moot is True


def test_a_page_holding_a_macro_is_not_exempt() -> None:
    """A macro is enough to make a page not lossless -- measured -- and that is
    what keeps the macro-in-a-table-cell specimen on the far side of this gate.
    That page is destroyed silently and its own loss report says nothing was
    lost, so a check that consulted the loss report alone would wave it through."""

    markdown = _round_trip(WITH_MACRO).replace("alpha paragraph", "alpha edited")
    result = _assess(WITH_MACRO, markdown)
    assert result.moot is False
    assert result.reason == "page_is_not_markdown_lossless"


def test_a_candidate_that_drops_a_named_loss_is_not_exempt() -> None:
    """Narrowed, not disarmed. Whatever the classification says, a candidate that
    actually drops something is a candidate someone has to agree to."""

    markdown = _round_trip(CELL_BACKGROUND)
    dropped = _candidate(CELL_BACKGROUND, markdown).replace(' data-highlight-colour="#ff0000"', "")
    result = assess_proof_mootness(CELL_BACKGROUND, dropped, markdown, options=OPTIONS)
    assert result.moot is False
    assert result.reason == "candidate_drops_something"


def test_the_fixed_mixed_list_is_lossless_and_needs_no_proof_exception() -> None:
    """The converter fix removes the reason for the old refusal.

    This is not a broader proof waiver. The candidate is byte-shape preserving,
    the page is now measured as Markdown-lossless, and the ordinary mootness
    conditions all agree.
    """
    markdown = _round_trip(WITH_LIST).replace("three item", "**three item**")
    candidate = _candidate(WITH_LIST, markdown)

    assert "<li><p>one item</p></li>" in WITH_LIST
    assert "<li><p>one item</p></li>" in candidate
    assert "<li>two item</li>" in candidate
    assert "<li><p><strong>three item</strong></p><p>and more</p></li>" in candidate

    result = assess_proof_mootness(WITH_LIST, candidate, markdown, options=OPTIONS)
    assert result.moot is True


def test_a_candidate_carrying_something_the_markdown_does_not_say_is_not_exempt() -> None:
    """The load-bearing check, stated on its own. A difference between the
    candidate and a plain render of the same Markdown came from the remote,
    through the splice -- which is the machinery whose attribution is in doubt."""

    markdown = _round_trip(PLAIN).replace("alpha paragraph", "alpha edited")
    smuggled = _candidate(PLAIN, markdown) + "<p>text that no markdown asked for</p>"
    result = assess_proof_mootness(PLAIN, smuggled, markdown, options=OPTIONS)
    assert result.moot is False
    assert result.reason == "candidate_differs_from_regeneration"


def test_a_page_that_cannot_be_read_is_not_exempt_and_does_not_raise() -> None:
    """A page we could not read is not a page we understand well enough to stop
    checking. It must come back as a closed verdict rather than an exception:
    this runs inside a refusal path, and a crash there replaces a refusal that
    explains itself with a traceback that does not."""

    result = assess_proof_mootness("<p>unclosed", "<p>unclosed", "unclosed", options=OPTIONS)
    assert result.moot is False
    assert result.reason == "candidate_unreadable"


# --------------------------------------------------------------------------
# Through the preflight, which is what publishes
# --------------------------------------------------------------------------


def test_an_edit_the_proof_refuses_is_still_refused_and_says_why_the_narrowing_missed(
    tmp_path,
) -> None:
    """Bolding text on a page that is not markdown-lossless was one of the measured
    over-blocks. It is still refused, and the refusal now names the condition that
    held -- otherwise a caller reading the same message twice cannot tell a gate that
    was considered from one that does not exist.

    **The pull's write policy is suspended here, on purpose.** §8.2 now refuses to
    write a canonical file for `WITH_LIST`, which grades `converter_fix_required` and
    has no approval route -- so this refusal is no longer reachable by pulling and
    then editing. The companion test below pins that.

    The subject of *this* test is the push proof, not the pull policy, so the grade
    is patched to let the file be written and nothing else is changed. The proof
    reads the page's storage, not its grade, so what it sees is exactly what it saw
    before. Measured substitutes were tried first and none of them works:
    `CELL_BACKGROUND` and `WITH_MACRO` are both pullable and the proof *allows* edits
    to them, which is the narrowing working correctly. Keeping the coverage therefore
    meant isolating the layer rather than finding another page.
    """

    import atlassian_skills.confluence.managed_pull as managed_pull_module
    from atlassian_skills.confluence.compatibility import compatibility_payload
    from atlassian_skills.confluence.migration_preflight import build_managed_preflight
    from atlassian_skills.confluence.pull_md import pull_md
    from atlassian_skills.core.errors import ValidationError
    from tests.unit.test_state_free_body_write import BodyClient

    def pullable(*args, **kwargs):
        return {**compatibility_payload(*args, **kwargs), "canonical_write_permitted": True}

    # Its own page, not `WITH_LIST`. C3 made mixed lists round-trip, so every edit in that
    # page became provable and this test stopped raising -- a real improvement, and not what
    # this test is for. It exists to pin that a *genuine* refusal still says why.
    #
    # A nested table: the shape `preservation._nests_a_table` refuses outright, because a
    # table inside a table cannot be spliced back. Both halves of the refusal are needed and
    # this shape has both -- a page that is merely unprovable gets waived as moot, and a page
    # that is merely lossy still proves. Chosen after measuring: an unclassifiable table cell,
    # an `ac:layout`, a `colgroup` and an `li data-uuid` all fail to refuse; a nested table and
    # a styled `span` both do.
    client = BodyClient()
    client.storage = (
        "<p>alpha paragraph text here</p>"
        "<table><tbody><tr><td>"
        "<table><tbody><tr><td>inner cell</td></tr></tbody></table>"
        "</td></tr></tbody></table>"
        "<p>bravo paragraph text here</p>"
    )
    managed = tmp_path / "page.md"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(managed_pull_module, "compatibility_payload", pullable)
        pull_md(client, "123", output_path=managed, portable=True, no_assets=True)
    managed.write_text(
        managed.read_text(encoding="utf-8").replace("alpha paragraph", "**alpha** paragraph"), encoding="utf-8"
    )

    with pytest.raises(ValidationError) as refused:
        build_managed_preflight(client, "123", managed)
    assert refused.value.context["reason"] == "ownership_proof_invalid"
    assert refused.value.context["proof_mootness"] == "page_is_not_markdown_lossless"
    assert client.puts == 0


def test_the_fixed_mixed_list_is_pullable_without_approval(tmp_path) -> None:
    """The supported flow exposes the converter fix all the way through pull.

    A green converter unit test is insufficient if atls still grades the page from
    an old assumption. The managed file is written, the grade is ready, and no
    approval action is offered because nothing is lost.
    """

    from tests.unit.conftest import pull_managed_accepting_named_losses
    from tests.unit.test_state_free_body_write import BodyClient

    client = BodyClient()
    client.storage = WITH_LIST
    managed = tmp_path / "page.md"

    result = pull_managed_accepting_named_losses(client, "123", managed, no_assets=True)

    assert result.status == "pulled"
    assert result.compatibility["status"] == "markdown_ready"
    assert managed.exists()
    assert client.puts == 0
    kinds = {step.get("kind") for step in result.edit_guidance}
    assert "approve_named_losses" not in kinds


def test_a_publish_that_the_proof_allows_does_not_claim_the_narrowing_fired(tmp_path) -> None:
    """`proof_waived` says the proof failed and was set aside, not that the page
    qualified. Reported either way, so a run over a corpus can count how often
    attribution actually decided anything -- a flag that appears only when true
    reads as absent rather than as false."""

    from atlassian_skills.confluence.migration_preflight import build_managed_preflight
    from tests.unit.conftest import pull_managed_accepting_named_losses
    from tests.unit.test_state_free_body_write import BodyClient

    client = BodyClient()
    client.storage = PLAIN
    managed = tmp_path / "page.md"
    pull_managed_accepting_named_losses(client, "123", managed, no_assets=True)
    managed.write_text(managed.read_text(encoding="utf-8").replace("alpha paragraph", "alpha edited"), encoding="utf-8")

    preflight = build_managed_preflight(client, "123", managed)
    assert preflight.ownership["proof_waived"] is False
    assert preflight.proof_mode == "full_migration"
    assert preflight.ownership["proof_mootness"]["moot"] is True
    assert client.puts == 0
