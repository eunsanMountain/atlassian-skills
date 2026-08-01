from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Literal

import cfxmark

from atlassian_skills.core.errors import ValidationError

# Task 4: Standard language alias map for Jira Server code blocks.
JIRA_CODE_LANGUAGE_MAP: dict[str, str] = {
    "js": "javascript",
    "ts": "typescript",
    "py": "python",
    "rb": "ruby",
    "sh": "bash",
    "yml": "yaml",
    "cs": "csharp",
    "cpp": "c++",
}

_MARKDOWN_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")


@dataclass(frozen=True)
class JiraMarkdownResult:
    """Converted Jira Markdown plus diagnostics kept outside the document body.

    cfxmark answers six questions about a conversion and this type used to carry
    two of them, so `attachments`, `push_safe`, `losses` and `diagnostics` were
    computed on every read and discarded at the boundary.

    `attachments` is the one that misleads a reader rather than merely leaving
    them uninformed. The Markdown says `![](design.png)` whether or not that file
    exists anywhere the caller can reach, so an agent summarises a picture it has
    never seen, and an agent that writes the body back publishes a reference to
    an attachment that is not there.

    `losses` is kept apart from `warnings` rather than concatenated into it. A
    warning is something to know; a loss is something gone, and a caller that has
    to tell them apart by reading the wording cannot.
    """

    markdown: str
    warnings: tuple[str, ...] = ()
    #: What the conversion could not carry across, as cfxmark names it.
    losses: tuple[str, ...] = ()
    #: Filenames the body references. Nothing here says they were downloaded.
    attachments: tuple[str, ...] = ()
    #: False when this Markdown must not be published back as the issue body.
    push_safe: bool = True
    #: cfxmark's structured account, passed through untouched for the JSON path.
    diagnostics: tuple[Any, ...] = ()
    #: The parsed body. Kept because it is the only grounded answer to "what text
    #: does this issue actually contain" -- the wiki source cannot be tokenised
    #: without counting macro names as words.
    document: Any = None

    @property
    def all_warnings(self) -> tuple[str, ...]:
        """Warnings and losses together, for callers that treat them alike.

        The two fields stay separate; this exists so that flattening them is a
        choice a call site makes rather than one this type makes for everybody.
        """

        return self.warnings + self.losses


class ReadableMarkdown(str):
    """In-process hint for content-only Markdown; serialized output stays plain text."""


@dataclass(frozen=True)
class WriteConversionResult:
    """Converted write body plus diagnostics that must reach the caller."""

    body: str
    warnings: tuple[str, ...] = ()
    losses: tuple[str, ...] = ()
    push_safe: bool = True


def _line_ending_warnings(source: str) -> tuple[str, ...]:
    fence_char = ""
    fence_length = 0
    for line in source.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        newline = line[len(content) :]
        if fence_char:
            if _is_markdown_fence_close(content, fence_char, fence_length):
                fence_char = ""
                fence_length = 0
                continue
            if "\r" in newline:
                return ("Markdown code block line endings were normalized to LF during conversion",)
            continue
        fence = _opening_markdown_fence(content)
        if fence is not None:
            fence_char = fence[0]
            fence_length = len(fence)
            continue
        if content.startswith(("    ", "\t")) and "\r" in newline:
            return ("Markdown code block line endings were normalized to LF during conversion",)
    return ()


def _is_markdown_fence_close(line: str, fence_char: str, fence_length: int) -> bool:
    stripped = line.lstrip(" ")
    candidate = stripped.strip()
    return (
        len(line) - len(stripped) <= 3
        and len(candidate) >= fence_length
        and bool(candidate)
        and set(candidate) == {fence_char}
    )


def _opening_markdown_fence(line: str) -> str | None:
    match = _MARKDOWN_FENCE_RE.match(line)
    if match is None:
        return None
    fence = match.group("fence")
    if fence[0] == "`" and "`" in match.group("info"):
        return None
    return fence


def format_page_md_header(title: str, space_key: str, version: Any) -> str:
    """Build a Markdown metadata header for a Confluence page."""
    ver = version if isinstance(version, int) else (version.number if version else "")
    return f"# {title}\n\n**Space:** {space_key}  **Version:** {ver}\n\n"


def format_page_md_document(header: str, markdown: str) -> str:
    """Add human-readable page metadata without embedding control state."""

    return header + markdown


def jira_wiki_to_md(source: str) -> str:
    """Convert Jira wiki markup to Markdown. Returns empty string on empty input."""

    return jira_wiki_to_md_result(source).markdown


def jira_wiki_to_md_result(source: str) -> JiraMarkdownResult:
    """Convert Jira wiki markup without appending diagnostics to Markdown."""

    if not source:
        return JiraMarkdownResult(markdown="")
    try:
        result = cfxmark.from_jira_wiki(source)
    except cfxmark.CfxmarkError as error:
        raise ValidationError(
            "Jira wiki markup could not be converted to Markdown",
            hint=str(error),
        ) from error
    return _from_cfxmark(result, result.markdown or "")


def _from_cfxmark(result: Any, markdown: str) -> JiraMarkdownResult:
    """Carry across everything cfxmark answered, not just the two fields.

    The Markdown is passed separately because callers post-process it -- section
    extraction, notice stripping -- and the diagnostics still describe the whole
    conversion that produced it.
    """

    return JiraMarkdownResult(
        markdown=markdown,
        warnings=tuple(result.warnings),
        losses=tuple(result.losses),
        attachments=tuple(result.attachments),
        push_safe=bool(result.push_safe),
        diagnostics=tuple(result.diagnostics),
        document=result.document,
    )


def _extract_section(md_text: str, section: str) -> str | None:
    """Extract content under a specific H2 heading from markdown text.

    Returns the content (stripped) if found, or None if not found.
    """
    lines = md_text.splitlines()
    in_section = False
    collected: list[str] = []
    fence_char = ""
    fence_length = 0
    for line in lines:
        if fence_char:
            if _is_markdown_fence_close(line, fence_char, fence_length):
                fence_char = ""
                fence_length = 0
            if in_section:
                collected.append(line)
            continue
        fence = _opening_markdown_fence(line)
        if fence is not None:
            fence_char = fence[0]
            fence_length = len(fence)
            if in_section:
                collected.append(line)
            continue
        # Match H2 heading (## heading)
        h2_match = re.match(r"^##\s+(.+)$", line)
        if h2_match:
            if in_section:
                break
            if h2_match.group(1).strip() == section:
                in_section = True
            continue
        if in_section:
            collected.append(line)
    if not in_section:
        return None
    return "\n".join(collected).strip()


def _drop_notice_lines(md_text: str, prefixes: list[str]) -> str:
    """Strip lines that start with any of the given prefixes."""
    result_lines = []
    for line in md_text.splitlines():
        if not any(line.startswith(p) for p in prefixes):
            result_lines.append(line)
    return "\n".join(result_lines)


def jira_wiki_to_md_with_options(
    wiki_text: str,
    *,
    section: str | None = None,
    heading_promotion: str | None = None,
    drop_leading_notice: list[str] | None = None,
    skip_conversion: bool = False,
) -> str:
    """Convert Jira wiki to md with output control options.

    - section: extract content under the given H2 heading (post-processing)
    - heading_promotion: documented for future use; not yet implemented in cfxmark
    - drop_leading_notice: strip lines matching any of the given prefix strings (post-processing)
    - skip_conversion: if True, skip wiki→md conversion but still apply section/notice extraction
      (useful when body_repr already converted the body)
    """
    return jira_wiki_to_md_with_options_result(
        wiki_text,
        section=section,
        heading_promotion=heading_promotion,
        drop_leading_notice=drop_leading_notice,
        skip_conversion=skip_conversion,
    ).markdown


def jira_wiki_to_md_with_options_result(
    wiki_text: str,
    *,
    section: str | None = None,
    heading_promotion: str | None = None,
    drop_leading_notice: list[str] | None = None,
    skip_conversion: bool = False,
) -> JiraMarkdownResult:
    """Convert Jira wiki markup and retain conversion diagnostics."""

    del heading_promotion
    if not wiki_text:
        return JiraMarkdownResult(markdown="")
    converted: Any = None
    warnings: tuple[str, ...] = ()
    if skip_conversion:
        md = wiki_text
    else:
        try:
            converted = cfxmark.from_jira_wiki(wiki_text)
            md = converted.markdown or ""
        except Exception as e:
            # The original is returned rather than nothing, and `push_safe`
            # stays false: a body that could not be converted is a body nobody
            # should publish back as though it had been.
            warnings = (f"cfxmark conversion failed; original Jira wiki retained: {e}",)
            md = wiki_text
            return JiraMarkdownResult(markdown=md, warnings=warnings, push_safe=False)

    if drop_leading_notice:
        md = _drop_notice_lines(md, drop_leading_notice)

    if section is not None:
        extracted = _extract_section(md, section)
        if extracted is None:
            # Signal not-found to caller via special sentinel
            raise _SectionNotFoundError(section)
        md = extracted

    if converted is None:
        # `skip_conversion` -- the body arrived already converted, so there is
        # no conversion here to have diagnostics about.
        return JiraMarkdownResult(markdown=md, warnings=warnings)
    return _from_cfxmark(converted, md)


class _SectionNotFoundError(Exception):
    """Internal signal: requested section not found in markdown output."""

    def __init__(self, section: str) -> None:
        super().__init__(section)
        self.section = section


def md_to_jira_wiki(
    source: str,
    *,
    heading_promotion: str = "jira",
    passthrough_prefixes: list[str] | None = None,
) -> str:
    """Convert Markdown to Jira wiki markup."""
    return md_to_jira_wiki_result(
        source,
        heading_promotion=heading_promotion,
        passthrough_prefixes=passthrough_prefixes,
    ).body


def md_to_jira_wiki_result(
    source: str,
    *,
    heading_promotion: str = "jira",
    passthrough_prefixes: list[str] | None = None,
) -> WriteConversionResult:
    """Convert Markdown to Jira wiki markup and retain diagnostics."""
    if not source:
        return WriteConversionResult(body="")
    opts: cfxmark.ConversionOptions | None = None
    if passthrough_prefixes:
        opts = cfxmark.ConversionOptions(passthrough_html_comment_prefixes=tuple(passthrough_prefixes))
    try:
        result = cfxmark.to_jira_wiki(
            source,
            input_format="markdown",
            heading_promotion=heading_promotion,  # type: ignore[arg-type]
            code_language_map=JIRA_CODE_LANGUAGE_MAP,
            options=opts,
        )
    except cfxmark.CfxmarkError as error:
        raise ValidationError(
            "Markdown could not be converted to Jira wiki markup",
            hint=str(error),
        ) from error
    return WriteConversionResult(
        body=result.jira_wiki or "",
        warnings=_line_ending_warnings(source) + result.warnings,
        losses=result.losses,
        push_safe=result.push_safe,
    )


def confluence_storage_to_md_result(
    xhtml: str,
    *,
    profile: Literal["editable", "readable"] = "readable",
    passthrough_prefixes: tuple[str, ...] = (),
) -> cfxmark.ConversionResult:
    """Convert storage XHTML and retain conversion diagnostics."""

    if not xhtml:
        if profile == "readable":
            return cfxmark.ConversionResult(
                markdown="",
                push_safe=False,
            )
        return cfxmark.ConversionResult(
            markdown="",
            push_safe=True,
        )
    result = cfxmark.to_md(
        xhtml,
        options=cfxmark.ConversionOptions(
            profile=profile,
            passthrough_html_comment_prefixes=passthrough_prefixes,
        ),
    )
    if profile == "readable":
        result = replace(
            result,
            push_safe=False,
        )
    return result


def confluence_storage_to_md(
    xhtml: str,
    *,
    profile: Literal["editable", "readable"] = "readable",
) -> str:
    """Convert Confluence storage XHTML to a selected Markdown profile."""

    if not xhtml:
        return ""
    markdown = confluence_storage_to_md_result(xhtml, profile=profile).markdown or ""
    return ReadableMarkdown(markdown) if profile == "readable" else markdown


def md_to_confluence_storage(
    source: str,
    *,
    passthrough_prefixes: tuple[str, ...] = (),
) -> str:
    """Convert Markdown to Confluence Storage Format XHTML."""
    return md_to_confluence_storage_result(
        source,
        passthrough_prefixes=passthrough_prefixes,
    ).body


def md_to_confluence_storage_result(
    source: str,
    *,
    passthrough_prefixes: tuple[str, ...] = (),
) -> WriteConversionResult:
    """Convert Markdown to storage XHTML and retain diagnostics."""
    if not source:
        return WriteConversionResult(body="")
    try:
        result = cfxmark.to_cfx(
            source,
            options=cfxmark.ConversionOptions(
                profile="editable",
                passthrough_html_comment_prefixes=passthrough_prefixes,
            ),
        )
    except cfxmark.CfxmarkError as error:
        raise ValidationError(
            "Markdown could not be converted to Confluence storage",
            hint=str(error),
        ) from error
    return WriteConversionResult(
        body=result.xhtml or "",
        warnings=_line_ending_warnings(source) + result.warnings,
        losses=result.losses,
        push_safe=result.push_safe,
    )


def _extract_name(value: Any) -> str:
    """Extract display name from a field that may be a string, dict, or None."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("display_name", value.get("displayName", value.get("name", ""))))
    return str(value)


def format_md_issue(issue: dict[str, Any], *, skip_body_conversion: bool = False) -> str:
    """Format a Jira issue dict as Markdown with description converted from wiki markup.

    When *skip_body_conversion* is True, the description is used as-is (already
    converted or intentionally kept in its original representation by --body-repr).
    """
    key = issue.get("key", "")
    summary = issue.get("summary", "")
    status = _extract_name(issue.get("status"))
    issuetype = _extract_name(issue.get("issuetype") or issue.get("issue_type") or issue.get("type"))
    priority = _extract_name(issue.get("priority"))
    assignee = _extract_name(issue.get("assignee"))
    description_raw = issue.get("description", "")
    if skip_body_conversion:
        description_md = description_raw
    else:
        description_md = jira_wiki_to_md(description_raw) if description_raw else ""

    lines = [
        f"# {key}: {summary}",
        "",
        f"**Type:** {issuetype}  **Status:** {status}  **Priority:** {priority}  **Assignee:** {assignee}",
    ]
    if description_md:
        lines += ["", "## Description", "", description_md]
    return "\n".join(lines)
