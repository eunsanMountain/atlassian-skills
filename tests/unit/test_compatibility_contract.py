"""The payload an agent reads to decide what it may do to a page.

Every assertion here is about a promise someone downstream relies on, so each
test says which promise and what breaks without it. The three that matter most:

* an agent must never see a non-zero exit for a page that is merely unpublishable
  from Markdown -- that is a classification, and an agent reads a failed exit as
  a broken command;
* an agent must never be told to invent a command, because that is the dead end
  this whole design exists to remove;
* a converter defect must never be dressed up as something for the author to
  approve.
"""

from __future__ import annotations

import cfxmark
import pytest

from atlassian_skills.confluence.compatibility import (
    SCHEMA,
    STATUS_BY_CLASSIFICATION,
    compatibility_payload,
)
from atlassian_skills.confluence.preservation import PreservationCapability

# Synthetic. A real page id in a test fixture is an internal identifier that
# ships with the package, and the pre-release review exists to catch exactly
# this -- it caught this one.
PAGE = "100000001"

MACRO = (
    '<ac:structured-macro ac:name="info" ac:schema-version="1"{extra}>'
    "<ac:rich-text-body><p>note body</p></ac:rich-text-body>"
    "</ac:structured-macro>"
)
CELL_BACKGROUND = (
    "<table><thead><tr><th>h</th></tr></thead>"
    '<tbody><tr><td data-highlight-colour="#ff0000">a</td></tr></tbody></table>'
)
UNCLASSIFIABLE = '<table><thead><tr><th>h</th></tr></thead><tbody><tr><td colspan="2">a</td></tr></tbody></table>'


def test_a_page_markdown_can_hold_is_ready() -> None:
    payload = compatibility_payload(PAGE, "<p>hello</p><ul><li>one</li></ul>")
    assert payload["schema"] == SCHEMA
    assert payload["status"] == "markdown_ready"
    assert payload["workflow_decision_required"] is False
    assert payload["findings"] == []


def test_a_macro_id_asks_for_no_approval_but_demands_the_managed_path() -> None:
    """Nothing here is the author's to decide -- they lose nothing as long as the
    identity is carried. What they must not do is publish without carrying it,
    which is what `requires_identity_carry` exists to say."""

    payload = compatibility_payload(PAGE, MACRO.format(extra=' ac:macro-id="7f3a-0001"'))
    assert payload["status"] == "markdown_identity_bound"
    assert payload["workflow_decision_required"] is False
    assert payload["requires_identity_carry"] is True


def test_a_named_loss_asks_for_approval_and_says_what_is_lost() -> None:
    payload = compatibility_payload(PAGE, CELL_BACKGROUND)
    assert payload["status"] == "migration_required"
    assert payload["workflow_decision_required"] is True
    codes = {finding["code"] for finding in payload["findings"]}
    assert "td@data-highlight-colour" in codes


def test_the_three_way_mixed_list_is_now_markdown_ready() -> None:
    """The mixed-list converter gap is closed rather than reclassified.

    A per-item list marker now records exactly which items had paragraph wrappers,
    including the formerly failing three-way combination of wrapped, bare and
    multi-paragraph items. The author is neither blocked nor asked to approve a
    converter defect.
    """
    payload = compatibility_payload(
        PAGE, "<ul><li><p>alpha bravo</p></li><li>delta echo</li><li><p>foxtrot</p><p>golf</p></li></ul>"
    )
    assert payload["status"] == "markdown_ready"
    assert payload["workflow_decision_required"] is False
    assert payload["findings"] == []


def test_an_unclassifiable_page_is_sent_to_the_storage_workflow() -> None:
    payload = compatibility_payload(PAGE, UNCLASSIFIABLE)
    assert payload["status"] == "xhtml_required"
    assert payload["recommended_workflow"] == "xhtml"


def test_an_unspecified_capability_shape_never_grants_permission_from_codes_alone() -> None:
    """A new capability must implement its shape check before it can unlock a
    managed Markdown file. Matching code names alone is intentionally not a
    preservation contract."""

    capability = PreservationCapability(
        name="unregistered-test",
        codes=frozenset({"td#topology"}),
        protects=("test",),
        closed_by="test",
    )

    assert (
        capability.covers(
            [{"code": "td#topology"}],
            "<table><tbody><tr><td>cell</td></tr></tbody></table>",
            converter="cfxmark 0.5.2",
            profile="editable",
        )
        is False
    )


# --------------------------------------------------------------------------
# Promises the payload makes to whoever reads it
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "storage",
    ["<p>hello</p>", MACRO.format(extra=' ac:macro-id="a"'), CELL_BACKGROUND, UNCLASSIFIABLE],
    ids=["ready", "identity", "named-loss", "unknown"],
)
def test_every_next_action_is_a_runnable_command(storage: str) -> None:
    """ "Use the XHTML workflow" leaves the reader to invent a command, and an
    invented command is how agents ended up stuck. Every step is argv."""

    payload = compatibility_payload(PAGE, storage)
    assert payload["next_actions"], "a status with no way forward is a dead end"
    for action in payload["next_actions"]:
        assert action["argv"][0] == "confluence"
        assert PAGE in action["argv"]
        assert action["label"]
        assert isinstance(action["requires_user_approval"], bool)


@pytest.mark.parametrize(
    "storage",
    ["<p>hello</p>", CELL_BACKGROUND, UNCLASSIFIABLE],
    ids=["ready", "named-loss", "unknown"],
)
def test_the_candidate_hash_is_stable_for_the_same_page(storage: str) -> None:
    """Approval is bound to an exact candidate. If the hash moved between two
    assessments of the same bytes, an approval could never be checked against
    what it approved."""

    assert (
        compatibility_payload(PAGE, storage)["candidate_sha256"]
        == (compatibility_payload(PAGE, storage)["candidate_sha256"])
    )


def test_explained_differences_are_not_listed_as_findings() -> None:
    """A macro's schema-version is dropped on every macro-bearing page and costs
    nothing, because the server stamps it back. Listing it would bury the
    findings that need a decision under one that never does."""

    payload = compatibility_payload(PAGE, MACRO.format(extra=""))
    assert payload["status"] == "markdown_ready"
    assert payload["findings"] == []


def test_every_supported_list_wrapper_combination_round_trips_without_findings() -> None:
    """The list marker covers simple and repeated mixed shapes, not one fixture.

    The old count assertion measured the converter gap. Keeping a manufactured
    finding after fixing the converter would make the release contract demand the
    bug. These cases retain the useful boundary: every wrapper arrangement that
    previously produced one or three findings now produces none.
    """

    for round_trips in (
        "<ul><li><p>a</p></li></ul>",
        "<ul><li><p>a</p></li><li>b</li></ul>",
        "<ul><li><p>a</p><p>b</p></li><li>c</li></ul>",
        "<ul><li><p>a</p></li><li>b</li><li><p>c</p><p>d</p></li></ul>",
        "<ul><li><p>a</p></li><li><p>e</p></li><li><p>f</p></li><li>b</li><li><p>c</p><p>d</p></li></ul>",
    ):
        assert compatibility_payload(PAGE, round_trips)["findings"] == [], round_trips


def test_the_status_table_covers_every_classification() -> None:
    """The mapping is data so the CLI, the Skill and any downstream adapter read one
    copy. A classification missing from it would raise at the worst moment --
    on a real page, mid-publish."""

    from cfxmark import compatibility as measured

    classes = {
        measured.MARKDOWN_LOSSLESS,
        measured.MARKDOWN_IDENTITY_BOUND,
        measured.NAMED_LOSS_CONSENTABLE,
        measured.CONVERTER_GAP_BLOCKED,
        measured.UNKNOWN_BLOCKED,
    }
    assert classes == set(STATUS_BY_CLASSIFICATION)


def test_a_page_that_cannot_be_converted_is_reported_not_raised() -> None:
    """This runs on every pull. A crash here would take out the pull rather than
    telling the caller the page is not Markdown-publishable."""

    payload = compatibility_payload(PAGE, "<p>unclosed")
    assert payload["status"] == "xhtml_required"
    assert payload["conversion_error"]


# --------------------------------------------------------------------------
# What may travel in an error envelope
# --------------------------------------------------------------------------


def test_the_digest_carries_no_leaf_values() -> None:
    """Error contexts are logged and displayed, and this project denies leaf
    values crossing that boundary by default.

    The full payload carries semantic paths, a summary sentence and a candidate
    hash. None of those belong in an error. What survives is a classification, a
    finding code and a count -- a name we chose and an integer."""

    from atlassian_skills.confluence.compatibility import compatibility_digest

    digest = compatibility_digest(PAGE, CELL_BACKGROUND)
    # `attention_required` is a boolean, and the one thing a caller reading a
    # refusal most needs: whether this page needed a decision at all.
    assert set(digest) == {
        "attention_required",
        "status",
        "workflow_decision_required",
        "requires_identity_carry",
        "recommended_workflow",
        "findings",
    }
    for finding in digest["findings"]:
        assert set(finding) == {"code", "count"}


def test_the_digest_still_says_which_class_and_how_many() -> None:
    """Reduced, not emptied. A refusal that says nothing about the page leaves the
    caller exactly where the old refusals did."""

    from atlassian_skills.confluence.compatibility import compatibility_digest

    digest = compatibility_digest(PAGE, CELL_BACKGROUND)
    assert digest["status"] == "migration_required"
    assert ("td@data-highlight-colour", 1) in {(f["code"], f["count"]) for f in digest["findings"]}


def test_a_fresh_precomputed_base_produces_the_same_compatibility_contract() -> None:
    storage = (
        "<table><thead><tr><th>key</th><th>value</th></tr></thead><tbody>"
        "<tr><td>a</td><td>b</td></tr>"
        "<tr><td>prefix</td><td>left</td><td>delta</td><td>right</td></tr>"
        "</tbody></table><p>After</p>"
    )
    base = cfxmark.to_md_artifact(storage, options=cfxmark.ConversionOptions(profile="editable"))

    ordinary = compatibility_payload(PAGE, storage, document_path="/tmp/page.md")
    precomputed = compatibility_payload(
        PAGE,
        storage,
        document_path="/tmp/page.md",
        base_artifact=base,
    )

    assert precomputed == ordinary


def test_a_precomputed_base_from_other_storage_is_rejected() -> None:
    base = cfxmark.to_md_artifact("<p>other</p>", options=cfxmark.ConversionOptions(profile="editable"))

    with pytest.raises(ValueError, match="does not belong"):
        compatibility_payload(PAGE, "<p>expected</p>", base_artifact=base)
