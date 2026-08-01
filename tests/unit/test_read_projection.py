"""Whether a reader can tell that the Markdown they got is not all of the page.

Measured over the 55-page corpus, on the profile `get --body-repr=md` actually
uses: 34 pages are faithful, 8 lose only their shape, and **13 lose text**. Close
to one page in four hands back a summary with a hole in it, and until now nothing
said so -- not the exit code, not `editable=false`, not the conversion warnings.

The three verdicts are kept apart on purpose. If every lossy page said "go and
read the storage", 21 pages would say it and nobody would. The thirteen that
genuinely lose text are worth interrupting for precisely because they are
thirteen and not fifty-five.

Ordered by what it costs to be wrong. Reporting a faithful page as incomplete
trains the reader to ignore the warning, so that comes first -- and the section
after it is the one that says why the verdict cannot be a search of the output.
"""

from __future__ import annotations

import pytest

from atlassian_skills.confluence.read_projection import assess_read_projection
from atlassian_skills.core.format.markdown import confluence_storage_to_md_result

FAITHFUL = {
    "plain prose": "<p>alpha bravo charlie delta</p>",
    "expand macro": (
        '<ac:structured-macro ac:name="expand"><ac:parameter ac:name="title">Details</ac:parameter>'
        "<ac:rich-text-body><p>inside the expand</p></ac:rich-text-body></ac:structured-macro>"
    ),
    "code macro": (
        '<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[print(1)]]>'
        "</ac:plain-text-body></ac:structured-macro>"
    ),
    "cell background": (
        "<table><thead><tr><th>heading</th></tr></thead>"
        '<tbody><tr><td data-highlight-colour="#ff0000">alpha value</td></tr></tbody></table>'
    ),
}
NESTED_EXPAND = (
    "<table><tbody><tr><td>outer cell</td><td>"
    '<ac:structured-macro ac:name="expand"><ac:rich-text-body><p>hidden body text</p>'
    "</ac:rich-text-body></ac:structured-macro></td></tr></tbody></table>"
)


def _assess(storage: str):
    conversion = confluence_storage_to_md_result(storage, profile="readable", passthrough_prefixes=())
    return assess_read_projection(conversion.document), conversion.markdown or ""


def _in_a_cell(inner: str) -> str:
    """A macro inside a table cell -- the shape that actually loses a body.

    Written as a helper because the four poison cases below differ only in the
    body text, and that is the whole point of them.
    """

    return f"<table><tbody><tr><td>outer cell</td><td>{inner}</td></tr></tbody></table>"


def _expand(body: str) -> str:
    return (
        f'<ac:structured-macro ac:name="expand"><ac:rich-text-body><p>{body}</p>'
        "</ac:rich-text-body></ac:structured-macro>"
    )


# --------------------------------------------------------------------------
# Saying nothing, when there is nothing to say
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(FAITHFUL))
def test_a_faithful_projection_reports_nothing(name: str) -> None:
    """The most expensive way to be wrong. A tool that warns on every read is a
    tool whose warnings get skipped, including the one that mattered."""

    report, _ = _assess(FAITHFUL[name])
    assert report.content_complete is True
    assert report.structure_complete is True
    assert report.attention_required is False
    assert report.reason is None


def test_a_cell_background_is_not_a_reason_to_reread() -> None:
    """Presentation, not content. The words are all here, and sending a reader to
    the storage for a colour is how the signal loses its meaning."""

    report, markdown = _assess(FAITHFUL["cell background"])
    assert "alpha value" in markdown
    assert report.attention_required is False


# --------------------------------------------------------------------------
# The case this exists for
# --------------------------------------------------------------------------


def test_a_macro_inside_a_table_loses_its_body_and_says_so() -> None:
    """The measured failure. The cell renders as a placeholder and the macro's
    text is simply not in the output -- so an agent summarising from it omits a
    paragraph and has no way to know."""

    report, markdown = _assess(NESTED_EXPAND)
    assert "hidden body text" not in markdown
    assert report.content_complete is False
    assert report.reason == "content_incomplete"


def test_the_omission_says_which_element_and_where() -> None:
    """ "Something is missing" sends the reader back to guess. The point is that
    they do not have to."""

    report, _ = _assess(NESTED_EXPAND)
    (omission,) = report.omissions
    assert omission.code == "element_body_omitted"
    assert omission.content_lost is True
    assert "table" in omission.semantic_path
    assert "structured-macro" in omission.semantic_path


def test_content_loss_offers_both_the_rendered_and_the_exact_read() -> None:
    """`view` first, because a summary is about what a person would see; storage
    second, for a caller that needs the markup. Both are reads, so neither waits
    on anyone's approval -- asking is what would stop an agent doing it."""

    report, _ = _assess(NESTED_EXPAND)
    actions = report.to_dict("123")["next_actions"]
    reprs = [item for action in actions for item in action["argv"] if item.startswith("--body-repr")]
    assert reprs == ["--body-repr=view", "--body-repr=storage"]
    assert all(action["requires_user_approval"] is False for action in actions)
    assert not [item for action in actions for item in action["argv"] if item.startswith("<")]


# --------------------------------------------------------------------------
# Two kinds of incompleteness, kept apart
# --------------------------------------------------------------------------


def test_a_faithful_page_carries_the_keys_rather_than_omitting_them() -> None:
    """Absent reads as "this version does not report it". False reads as "we
    looked"."""

    report, _ = _assess(FAITHFUL["plain prose"])
    payload = report.to_dict("123")
    assert payload["content_complete"] is True
    assert payload["structure_complete"] is True
    assert payload["attention_required"] is False
    assert payload["attention_reason"] is None
    assert payload["omissions"] == []
    # Nothing to do, so nothing is suggested.
    assert "next_actions" not in payload


def test_an_entity_is_text_and_not_a_loss() -> None:
    """The bug that made this measurement lie. Comparing an encoded source
    against a decoded output reported every `&quot;` on the page as missing
    content -- three of the first four "losses" found were this, not the
    converter."""

    report, _ = _assess("<p>alpha &quot;quoted&quot; bravo &amp; charlie</p>")
    assert report.content_complete is True
    assert report.attention_required is False


# --------------------------------------------------------------------------
# Why the verdict cannot be a word search
#
# Every case below renders as exactly `[cfx:expand]`. The body is gone in all of
# them, and a search of the output for the body's words reported three of the
# four as complete -- once because the words were too short, once because they
# were digits, once because the same phrase appeared in a paragraph elsewhere.
# --------------------------------------------------------------------------


POISON = {
    "short CJK words": "중요 경고",
    "digits only": "1234 5678",
    "one short word": "n/a",
    "long ASCII words": "critical warning",
}


@pytest.mark.parametrize("name", sorted(POISON))
def test_a_lost_body_is_reported_however_it_is_spelled(name: str) -> None:
    """Korean runs to two- and three-syllable words, so this is not a rare edge.
    A reader handed the projection loses the warning and is told nothing."""

    report, markdown = _assess(_in_a_cell(_expand(POISON[name])))
    assert "[cfx:expand]" in markdown
    assert POISON[name] not in markdown
    assert report.content_complete is False, f"{name} was excused from the check"
    assert report.reason == "content_incomplete"


def test_the_same_phrase_elsewhere_does_not_cover_for_the_loss() -> None:
    """The failure mode a word search cannot avoid: it asks whether the word is
    anywhere in the output, and a technical page repeats its terms. The more a
    document reuses its vocabulary, the more of it could go missing quietly."""

    storage = "<p>critical warning</p>" + _in_a_cell(_expand("critical warning"))
    report, markdown = _assess(storage)
    assert markdown.count("critical warning") == 1  # the paragraph, not the macro
    assert report.content_complete is False


def test_an_element_carrying_no_text_is_only_a_shape_change() -> None:
    """The other half. If everything the element said lives in its attributes --
    a mention, an attachment link -- then nothing a reader reads has gone, and
    saying otherwise is what makes the warning worth ignoring.

    On the corpus these are the eight shape-only pages: 32 `@user` mentions and
    a long tail of attachment and date links.
    """

    storage = '<p>alpha <ac:link><ri:user ri:userkey="0123456789abcdef"/></ac:link> bravo</p>'
    report, markdown = _assess(storage)
    assert markdown.strip() == "alpha [@user] bravo"
    assert report.structure_complete is False
    assert report.content_complete is True
    assert [item.code for item in report.omissions] == ["element_shape_replaced"]


def test_an_attachment_link_keeps_its_filename_and_is_not_a_content_loss() -> None:
    """The long tail of the shape-only pages. The filename is the label, so the
    reader still knows what was linked."""

    report, markdown = _assess('<p>see <ac:link><ri:attachment ri:filename="deck.pptx"/></ac:link> please</p>')
    assert "deck.pptx" in markdown
    assert report.content_complete is True


def test_the_projection_really_does_drop_opaque_bodies() -> None:
    """The assumption the whole verdict rests on, pinned rather than assumed.

    Deciding from the source alone is only sound because this projection renders
    an opaque element as its label and nothing else. If cfxmark ever starts
    emitting the body, this fails and the rule needs revisiting -- which is the
    point of asserting it here rather than trusting a docstring.
    """

    _, markdown = _assess(_in_a_cell(_expand("alpha bravo charlie delta")))
    assert "alpha" not in markdown
    assert "[cfx:expand]" in markdown


def test_an_identifier_is_not_reported_lost_when_the_page_keeps_it() -> None:
    """Ordinary prose still has to come back clean. Markdown escapes `frame_idx`
    to `frame\\_idx`, and an earlier version compared the two spellings and
    called every identifier on the page missing."""

    report, markdown = _assess("<p>the field frame_idx holds the value</p>")
    assert "frame" in markdown
    assert report.content_complete is True
    assert report.attention_required is False


def test_reader_text_ignores_how_the_element_is_spelled() -> None:
    """Attribute values are markup. Counting `ac:name="expand"` as text would
    make every macro look like it carried something."""

    from atlassian_skills.confluence.read_projection import reader_text

    assert reader_text('<ac:structured-macro ac:name="expand"/>') == ""
    assert (
        reader_text(
            '<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[x = 1]]>'
            "</ac:plain-text-body></ac:structured-macro>"
        )
        == "x = 1"
    )
