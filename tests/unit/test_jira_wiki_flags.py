from __future__ import annotations

import pytest

from atlassian_skills.core.format.markdown import (
    _drop_notice_lines,
    _extract_section,
    _SectionNotFoundError,
    jira_wiki_to_md_with_options,
)

# ---------------------------------------------------------------------------
# _extract_section
# ---------------------------------------------------------------------------


class TestExtractSection:
    WIKI_MD = "## Section1\ncontent1 line1\ncontent1 line2\n## Section2\ncontent2"

    def test_first_section(self) -> None:
        result = _extract_section(self.WIKI_MD, "Section1")
        assert result == "content1 line1\ncontent1 line2"

    def test_second_section(self) -> None:
        result = _extract_section(self.WIKI_MD, "Section2")
        assert result == "content2"

    def test_section_not_found_returns_none(self) -> None:
        result = _extract_section(self.WIKI_MD, "DoesNotExist")
        assert result is None

    def test_empty_text(self) -> None:
        result = _extract_section("", "Section1")
        assert result is None

    def test_section_with_leading_trailing_whitespace_in_heading(self) -> None:
        md = "##  Section1 \ncontent"
        # heading strip applies; match exact after strip
        result = _extract_section(md, "Section1")
        assert result == "content"


# ---------------------------------------------------------------------------
# _drop_notice_lines
# ---------------------------------------------------------------------------


class TestDropNoticeLines:
    def test_strips_matching_prefix(self) -> None:
        text = "NOTE: important\nnormal line\nWARNING: caution"
        result = _drop_notice_lines(text, ["NOTE:", "WARNING:"])
        assert result == "normal line"

    def test_no_match_keeps_all(self) -> None:
        text = "line1\nline2"
        result = _drop_notice_lines(text, ["NOTICE:"])
        assert result == "line1\nline2"

    def test_empty_prefixes_keeps_all(self) -> None:
        text = "line1\nline2"
        result = _drop_notice_lines(text, [])
        assert result == "line1\nline2"

    def test_empty_text(self) -> None:
        result = _drop_notice_lines("", ["NOTE:"])
        assert result == ""


# ---------------------------------------------------------------------------
# jira_wiki_to_md_with_options
# ---------------------------------------------------------------------------


class TestJiraWikiToMdWithOptions:
    WIKI_WITH_SECTIONS = "h2. Section1\ncontent1\nh2. Section2\ncontent2"

    def test_section_extraction(self) -> None:
        result = jira_wiki_to_md_with_options(self.WIKI_WITH_SECTIONS, section="Section1")
        assert "content1" in result
        assert "Section2" not in result
        assert "content2" not in result

    def test_section_not_found_raises(self) -> None:
        with pytest.raises(_SectionNotFoundError) as exc_info:
            jira_wiki_to_md_with_options(self.WIKI_WITH_SECTIONS, section="Missing")
        assert exc_info.value.section == "Missing"

    def test_drop_leading_notice(self) -> None:
        wiki = "NOTE: ignore this\nnormal content"
        result = jira_wiki_to_md_with_options(wiki, drop_leading_notice=["NOTE:"])
        assert "NOTE:" not in result
        assert "normal content" in result

    def test_no_options_plain_conversion(self) -> None:
        result = jira_wiki_to_md_with_options("*bold*")
        assert result  # non-empty

    def test_empty_input_returns_empty(self) -> None:
        result = jira_wiki_to_md_with_options("")
        assert result == ""

    def test_drop_notice_then_section(self) -> None:
        """drop_leading_notice is applied before section extraction."""
        wiki = "NOTE: header notice\nh2. Details\nactual content"
        result = jira_wiki_to_md_with_options(wiki, section="Details", drop_leading_notice=["NOTE:"])
        assert "NOTE:" not in result
        assert "actual content" in result
