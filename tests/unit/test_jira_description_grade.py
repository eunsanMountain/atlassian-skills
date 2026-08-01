"""Which descriptions may be managed as Markdown, and why the answer is narrow.

The grade decides whether a body converts safely, and the constant temptation is
to grade on how the markup LOOKS. The measured failure is the opposite: the
worst body in the corpus reported zero warnings and zero losses. It was written
in Markdown and stored as wiki, where `##` is a numbered-list marker, so the
headings became list items and the list items became escaped plain text --
silently, and with nothing for a shape test to notice.

So the grade comes from a round trip. Everything below is about what that round
trip is allowed to conclude.
"""

from __future__ import annotations

from dataclasses import dataclass

import cfxmark

from atlassian_skills.jira.description_grade import (
    CONVERTER_FIX_REQUIRED,
    MARKDOWN_IDENTITY_BOUND,
    MARKDOWN_READY,
    MIGRATION_REQUIRED,
    WIKI_REQUIRED,
    find_identity,
    grade_description,
)
from atlassian_skills.jira.read_projection import assess_jira_read


@dataclass
class _Report:
    content_complete: bool = True
    write_back_safe: bool = True
    losses: tuple[str, ...] = ()
    first_difference: tuple[str, str] | None = None


def _real_report(wiki: str) -> object:
    """The product's own round trip, so the grade is graded on real evidence."""

    result = cfxmark.from_jira_wiki(wiki)
    return assess_jira_read(
        wiki,
        result.markdown or "",
        document=getattr(result, "document", None),
        losses=tuple(result.losses or ()),
    )


# --------------------------------------------------------------------------
# Which identity is refused, and which is carried
#
# All three kinds were refused together until the round trip was measured per
# kind. They do not behave alike, and the grade now says so. Every test in this
# section runs the real converter, because that measurement is the only thing
# entitling the narrower rule to exist.
# --------------------------------------------------------------------------


def test_a_mention_is_refused_because_the_round_trip_deletes_it() -> None:
    """`[~alice] please review` reads as ` please review`. The person is gone.

    Regenerating the mention would mean looking the name up again, and the
    lookup can return the wrong person or nobody -- so there is no candidate a
    publish could prove, and `wiki_required` is the whole answer.
    """

    wiki = "[~alice] please review"
    grade = grade_description(wiki, read_report=_real_report(wiki))

    assert grade.status == WIKI_REQUIRED
    assert grade.status != MARKDOWN_IDENTITY_BOUND
    assert "identity_not_carried" in grade.reasons
    assert grade.identity_not_carried == ("user_mention",)
    assert grade.editable_as_markdown is False


def test_a_link_and_an_attachment_are_graded_identity_bound() -> None:
    """Measured, not assumed: both come back byte for byte.

    This is the grade the module used to withhold from every body. Withholding
    it from these two was not caution -- it refused every description carrying a
    link or a screenshot, which is most of them.
    """

    wiki = "h2. Overview\n\nSee [the design|https://example.test/d].\n\n!diagram.png!\n"
    grade = grade_description(wiki, read_report=_real_report(wiki))

    assert grade.status == MARKDOWN_IDENTITY_BOUND
    assert grade.editable_as_markdown is True
    assert grade.identity == ("attachment_reference", "smart_link")
    assert grade.identity_not_carried == ()
    assert grade.reasons == ()


def test_a_refusal_names_which_construct_caused_it() -> None:
    """`identity` lists everything in the body, so on its own it cannot answer
    the only question the author has: which one do I have to deal with?"""

    wiki = "See [the design|https://example.test/d], and [~alice] please review.\n"
    grade = grade_description(wiki, read_report=_real_report(wiki))

    assert grade.status == WIKI_REQUIRED
    assert "smart_link" in grade.identity
    assert grade.identity_not_carried == ("user_mention",)


def test_a_thumbnail_attachment_is_refused_by_the_round_trip_and_not_by_its_kind() -> None:
    """`!x.png|thumbnail!` comes back `!x.png|thumbnail=true!` -- the same image
    and not the same bytes.

    Pinned because the tempting fix is to put `attachment_reference` back in the
    not-carried set, which would also refuse every attachment that does come
    back intact. The round trip already catches this one, and that is the reason
    the grade is a round trip rather than a list of kinds.
    """

    wiki = "Screenshot: !diagram.png|thumbnail!\n"
    grade = grade_description(wiki, read_report=_real_report(wiki))

    assert grade.status == WIKI_REQUIRED
    assert "write_back_would_change_the_body" in grade.reasons
    assert "identity_not_carried" not in grade.reasons
    assert grade.identity_not_carried == ()


def test_an_attachment_with_an_explicit_parameter_still_round_trips() -> None:
    """The counterpart to the thumbnail case: `|width=400` is already in the
    spelling the converter emits, so it survives and the body stays manageable.
    Without this, the test above reads as "attachments are refused"."""

    wiki = "Screenshot: !diagram.png|width=400!\n"
    grade = grade_description(wiki, read_report=_real_report(wiki))

    assert grade.status == MARKDOWN_IDENTITY_BOUND
    assert grade.identity == ("attachment_reference",)


def test_every_identity_kind_is_recognised() -> None:
    """A mention, a smart link and an attachment reference all carry Jira-side
    identity that cannot be rebuilt from the rendered text. Whether that
    identity SURVIVES a round trip is the separate question above."""

    assert find_identity("[~bob] hi") == ("user_mention",)
    assert find_identity("[see this|https://example.test/x]") == ("smart_link",)
    assert find_identity("!diagram.png|thumbnail!") == ("attachment_reference",)


def test_a_body_with_no_identity_and_a_clean_round_trip_is_ready() -> None:
    """The narrowness has to have a bottom, or the grade refuses everything and
    the Markdown workflow is a promise nobody can use."""

    grade = grade_description("h2. Title\n\nplain paragraph\n", read_report=_Report())

    assert grade.status == MARKDOWN_READY
    assert grade.editable_as_markdown is True
    assert grade.reasons == ()


# --------------------------------------------------------------------------
# Round-trip evidence, not appearance
# --------------------------------------------------------------------------


def test_the_markdown_looking_body_is_refused_although_it_reports_no_loss() -> None:
    """The measured worst case, run through the real converter.

    `## 방향` is a numbered-list marker in Jira wiki, so this body reads as a
    list and the heading is gone -- with no warning and no loss to report. Only
    the round trip catches it, which is why the grade is built on one.
    """

    wiki = "## 방향\n# first\n# second\n"
    grade = grade_description(wiki, read_report=_real_report(wiki))

    assert grade.status == WIKI_REQUIRED
    assert "write_back_would_change_the_body" in grade.reasons


def test_a_body_that_would_come_back_different_is_refused() -> None:
    grade = grade_description("some body", read_report=_Report(write_back_safe=False, first_difference=("a", "b")))

    assert grade.status == WIKI_REQUIRED
    assert grade.first_difference == ("a", "b")


def test_named_losses_are_the_author_s_decision_not_a_refusal() -> None:
    """A named loss is something a person can look at and accept. Refusing it
    outright would put the author in front of a wall instead of a choice."""

    grade = grade_description("body", read_report=_Report(losses=("panel title dropped",)))

    assert grade.status == MIGRATION_REQUIRED
    assert grade.losses == ("panel title dropped",)


def test_a_converter_failure_is_ours_and_not_consentable() -> None:
    """Nobody should be asked to approve our defect, and calling it a migration
    would ask exactly that."""

    grade = grade_description("body", read_report=_Report(), conversion_failed=True)

    assert grade.status == CONVERTER_FIX_REQUIRED
    assert grade.reasons == ("conversion_failed",)


def test_a_mention_outranks_a_clean_round_trip() -> None:
    """Even told the round trip was clean, a mention is refused.

    The stub says `write_back_safe=True`, which is what a converter that learned
    to regenerate mentions from their rendered name would report. That is
    precisely the mechanism nobody should trust -- the name resolves to whoever
    holds it now -- so the refusal cannot be delegated to the round trip.
    """

    grade = grade_description("[~alice] ok", read_report=_Report(write_back_safe=True))

    assert grade.status == WIKI_REQUIRED
    assert grade.identity_not_carried == ("user_mention",)
    assert grade.editable_as_markdown is False


def test_a_named_loss_beside_carried_identity_is_still_a_migration() -> None:
    """Identity being carried does not make an unrelated loss disappear. The
    body is graded on the loss, and `migration_required` is not editable, so the
    author is asked before anything is published."""

    grade = grade_description(
        "See [the design|https://example.test/d]",
        read_report=_Report(losses=("panel title dropped",)),
    )

    assert grade.status == MIGRATION_REQUIRED
    assert grade.editable_as_markdown is False


def test_incomplete_content_never_grades_ready() -> None:
    grade = grade_description("body", read_report=_Report(content_complete=False))

    assert grade.status == WIKI_REQUIRED
    assert "content_incomplete" in grade.reasons
