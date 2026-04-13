from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlassian_skills.core.format.markdown import (
    _drop_notice_lines,
    _extract_section,
    _SectionNotFoundError,
    confluence_storage_to_md,
    jira_wiki_to_md,
    jira_wiki_to_md_with_options,
    md_to_confluence_storage,
    md_to_jira_wiki,
)
from atlassian_skills.jira.preprocessing import (
    normalize_smart_links,
    preprocess_jira_text,
    replace_mentions,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


def test_jira_wiki_roundtrip_converges() -> None:
    """wiki → md → wiki should converge within 2 iterations."""
    fixture = json.loads((FIXTURES / "jira/get-issue-proj3.json").read_text())
    original_wiki = fixture.get("description", "")
    if not original_wiki:
        return  # skip if description is empty/None

    # First round-trip
    md1 = jira_wiki_to_md(original_wiki)
    wiki1 = md_to_jira_wiki(md1)

    # Second round-trip
    md2 = jira_wiki_to_md(wiki1)
    wiki2 = md_to_jira_wiki(md2)

    # Should converge by 2nd iteration
    assert wiki1 == wiki2 or md1 == md2, "Round-trip did not converge within 2 iterations"


def test_jira_wiki_to_md_preserves_headings() -> None:
    """Basic heading conversion."""
    md = jira_wiki_to_md("h2. 방향\n내용")
    assert "방향" in md


def test_md_to_jira_wiki_basic() -> None:
    """Basic md → wiki conversion."""
    wiki = md_to_jira_wiki("## Heading\n\nParagraph text")
    assert "Heading" in wiki


# ---------------------------------------------------------------------------
# replace_mentions — multiple in one text, edge cases
# ---------------------------------------------------------------------------


def test_replace_mentions_multiple_in_one_text() -> None:
    text = "[~accountid:alice] reviewed by [~accountid:bob] and [~accountid:carol]"
    result = replace_mentions(text)
    assert result == "@user-alice reviewed by @user-bob and @user-carol"


def test_replace_mentions_empty_input() -> None:
    assert replace_mentions("") == ""


def test_replace_mentions_preserves_surrounding_text() -> None:
    text = "Please ping [~accountid:xyz] about this."
    result = replace_mentions(text)
    assert result == "Please ping @user-xyz about this."


def test_replace_mentions_accountid_with_special_chars() -> None:
    """Account IDs can contain digits, letters, and hyphens."""
    text = "[~accountid:5e7b9c1a-3d2e-4f5a-b6c7-8d9e0f1a2b3c]"
    result = replace_mentions(text)
    assert result == "@user-5e7b9c1a-3d2e-4f5a-b6c7-8d9e0f1a2b3c"


# ---------------------------------------------------------------------------
# normalize_smart_links — edge cases
# ---------------------------------------------------------------------------


def test_normalize_smart_links_empty_input() -> None:
    assert normalize_smart_links("") == ""


def test_normalize_smart_links_preserves_normal_two_part_links() -> None:
    text = "[normal|http://example.com]"
    assert normalize_smart_links(text) == text


def test_normalize_smart_links_mixed_content() -> None:
    text = "See [Docs|http://docs.example.com|smart-link] and plain text."
    result = normalize_smart_links(text)
    assert result == "See [Docs|http://docs.example.com] and plain text."


# ---------------------------------------------------------------------------
# preprocess_jira_text — edge cases
# ---------------------------------------------------------------------------


def test_preprocess_jira_text_empty_input() -> None:
    assert preprocess_jira_text("") == ""


def test_preprocess_jira_text_no_special_markup() -> None:
    text = "Just a plain sentence with no mentions or links."
    assert preprocess_jira_text(text) == text


def test_preprocess_jira_text_multiple_mentions_and_smart_links() -> None:
    text = (
        "[~accountid:alice] opened [Issue|http://jira/PROJ-1|smart-link] "
        "and [~accountid:bob] closed [PR|http://bb/pr/1|smart-link]."
    )
    result = preprocess_jira_text(text)
    assert "@user-alice" in result
    assert "@user-bob" in result
    assert "[Issue|http://jira/PROJ-1]" in result
    assert "[PR|http://bb/pr/1]" in result
    assert "smart-link" not in result.lower()


# ---------------------------------------------------------------------------
# jira_wiki_to_md — edge cases
# ---------------------------------------------------------------------------


def test_jira_wiki_to_md_empty_input() -> None:
    assert jira_wiki_to_md("") == ""


def test_jira_wiki_to_md_unicode_preserved() -> None:
    text = "h1. 한국어 헤딩\n\n유니코드 내용 ñoño émoji 🎉"
    result = jira_wiki_to_md(text)
    assert "한국어" in result
    assert "유니코드" in result


def test_jira_wiki_to_md_nested_list() -> None:
    wiki = "* Item 1\n** Nested 1\n** Nested 2\n* Item 2"
    result = jira_wiki_to_md(wiki)
    # Nested items should appear somewhere in output
    assert "Nested 1" in result
    assert "Nested 2" in result
    assert "Item 2" in result


# ---------------------------------------------------------------------------
# md_to_jira_wiki — edge cases
# ---------------------------------------------------------------------------


def test_md_to_jira_wiki_empty_input() -> None:
    assert md_to_jira_wiki("") == ""


def test_md_to_jira_wiki_special_chars_preserved() -> None:
    """Special characters should survive conversion."""
    text = "Use `~` for mention, `+` for insert, `-` for delete."
    result = md_to_jira_wiki(text)
    assert "~" in result or "mention" in result  # content preserved


# ---------------------------------------------------------------------------
# Parametrized jira_wiki_to_md conversions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wiki,expected_substr",
    [
        ("h1. Title", "# Title"),
        ("h2. Sub", "## Sub"),
        ("*bold*", "**bold**"),
        ("_italic_", "*italic*"),
        ("{code}print('hi'){code}", "print('hi')"),
        ("", ""),
    ],
)
def test_jira_wiki_to_md_conversions(wiki: str, expected_substr: str) -> None:
    result = jira_wiki_to_md(wiki)
    assert expected_substr in result


# ---------------------------------------------------------------------------
# Parametrized md_to_jira_wiki conversions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "md,expected_substr",
    [
        ("# Title", "h1."),
        ("**bold**", "*bold*"),
        ("", ""),
    ],
)
def test_md_to_jira_wiki_conversions(md: str, expected_substr: str) -> None:
    result = md_to_jira_wiki(md)
    assert expected_substr in result


# ---------------------------------------------------------------------------
# confluence_storage_to_md
# ---------------------------------------------------------------------------


def test_confluence_storage_basic_html() -> None:
    xhtml = "<p>Hello <strong>world</strong></p>"
    result = confluence_storage_to_md(xhtml)
    assert "Hello" in result
    assert "world" in result


def test_confluence_storage_empty() -> None:
    assert confluence_storage_to_md("") == ""


def test_confluence_storage_nested() -> None:
    xhtml = "<ul><li>Item A<ul><li>Nested B</li></ul></li></ul>"
    result = confluence_storage_to_md(xhtml)
    assert "Item A" in result
    assert "Nested B" in result


# ---------------------------------------------------------------------------
# md_to_confluence_storage
# ---------------------------------------------------------------------------


def test_md_to_storage_basic() -> None:
    result = md_to_confluence_storage("## Heading\n\nParagraph text.")
    assert "Heading" in result
    assert "Paragraph text" in result


def test_md_to_storage_empty() -> None:
    assert md_to_confluence_storage("") == ""


# ---------------------------------------------------------------------------
# heading promotion modes via md_to_jira_wiki
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["jira", "confluence", "none"])
def test_heading_promotion_modes(mode: str) -> None:
    md = "# H1 heading\n\n## H2 heading\n\nParagraph."
    result = md_to_jira_wiki(md, heading_promotion=mode)
    # Regardless of promotion mode the textual content must survive
    assert "H1 heading" in result
    assert "H2 heading" in result


# ---------------------------------------------------------------------------
# _extract_section
# ---------------------------------------------------------------------------


def test_extract_section_found() -> None:
    text = "## Overview\n\nSome overview content.\n\n## Details\n\nDetail text."
    result = _extract_section(text, "Overview")
    assert result is not None
    assert "Some overview content." in result


def test_extract_section_not_found_returns_none() -> None:
    text = "## Overview\n\nContent here."
    result = _extract_section(text, "NonExistent")
    assert result is None


def test_extract_section_stops_at_next_h2() -> None:
    text = "## First\n\nFirst content.\n\n## Second\n\nSecond content."
    result = _extract_section(text, "First")
    assert result is not None
    assert "First content." in result
    assert "Second content." not in result


# ---------------------------------------------------------------------------
# _drop_notice_lines
# ---------------------------------------------------------------------------


def test_drop_notice_lines_removes_prefixed() -> None:
    text = "[NOTE] This is a notice.\nNormal line.\n[NOTE] Another notice."
    result = _drop_notice_lines(text, ["[NOTE]"])
    assert "[NOTE]" not in result
    assert "Normal line." in result


def test_drop_notice_lines_empty_prefixes_keeps_all() -> None:
    text = "Line one.\nLine two."
    result = _drop_notice_lines(text, [])
    assert result == text


def test_drop_notice_lines_multiple_prefixes() -> None:
    text = "[WARN] warning\n[INFO] info\nkeep this"
    result = _drop_notice_lines(text, ["[WARN]", "[INFO]"])
    assert "warning" not in result
    assert "info" not in result
    assert "keep this" in result


# ---------------------------------------------------------------------------
# jira_wiki_to_md_with_options — section extraction
# ---------------------------------------------------------------------------


def test_jira_wiki_to_md_with_options_section_found() -> None:
    wiki = "h2. Summary\n\nSummary content.\n\nh2. Details\n\nDetail content."
    result = jira_wiki_to_md_with_options(wiki, section="Summary")
    assert "Summary content." in result
    assert "Detail content." not in result


def test_jira_wiki_to_md_with_options_section_not_found_raises() -> None:
    wiki = "h2. Overview\n\nContent."
    with pytest.raises(_SectionNotFoundError):
        jira_wiki_to_md_with_options(wiki, section="Missing")


def test_jira_wiki_to_md_with_options_drop_notice() -> None:
    wiki = "[NOTICE] This is a notice line.\n\nNormal content."
    result = jira_wiki_to_md_with_options(wiki, drop_leading_notice=["[NOTICE]"])
    assert "[NOTICE]" not in result
    assert "Normal content." in result


def test_jira_wiki_to_md_with_options_empty_input() -> None:
    assert jira_wiki_to_md_with_options("") == ""
