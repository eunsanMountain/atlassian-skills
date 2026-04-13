from __future__ import annotations

import re
from typing import Any

import cfxmark

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


def _lossy_footer(warnings: tuple[str, ...] | list[str]) -> str:
    """Return a lossy conversion footer if there are warnings, else empty string."""
    if warnings:
        return "\n\n[converted: jira-wiki→md, lossy]"
    return ""


def jira_wiki_to_md(source: str) -> str:
    """Convert Jira wiki markup to Markdown. Returns empty string on empty input."""
    if not source:
        return ""
    result = cfxmark.from_jira_wiki(source)
    md = result.markdown or ""
    md += _lossy_footer(result.warnings)
    return md


def _extract_section(md_text: str, section: str) -> str | None:
    """Extract content under a specific H2 heading from markdown text.

    Returns the content (stripped) if found, or None if not found.
    """
    lines = md_text.splitlines()
    in_section = False
    collected: list[str] = []
    for line in lines:
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
) -> str:
    """Convert Jira wiki to md with output control options.

    - section: extract content under the given H2 heading (post-processing)
    - heading_promotion: documented for future use; not yet implemented in cfxmark
    - drop_leading_notice: strip lines matching any of the given prefix strings (post-processing)
    """
    if not wiki_text:
        return ""
    try:
        result = cfxmark.from_jira_wiki(wiki_text)
        md = result.markdown or ""
        md += _lossy_footer(result.warnings)
    except Exception as e:
        import sys

        print(f"[atls] warning: cfxmark conversion failed: {e}", file=sys.stderr)
        md = wiki_text  # fallback to raw

    if drop_leading_notice:
        md = _drop_notice_lines(md, drop_leading_notice)

    if section is not None:
        extracted = _extract_section(md, section)
        if extracted is None:
            # Signal not-found to caller via special sentinel
            raise _SectionNotFoundError(section)
        md = extracted

    return md


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
    if not source:
        return ""
    opts: cfxmark.ConversionOptions | None = None
    if passthrough_prefixes:
        opts = cfxmark.ConversionOptions(passthrough_html_comment_prefixes=tuple(passthrough_prefixes))
    result = cfxmark.to_jira_wiki(
        source,
        input_format="markdown",
        heading_promotion=heading_promotion,  # type: ignore[arg-type]
        code_language_map=JIRA_CODE_LANGUAGE_MAP,
        options=opts,
    )
    return result.jira_wiki or ""


def confluence_storage_to_md(xhtml: str) -> str:
    """Convert Confluence Storage Format XHTML to Markdown."""
    if not xhtml:
        return ""
    result = cfxmark.to_md(xhtml)
    md = result.markdown or ""
    if result.warnings:
        md += "\n\n[converted: storage→md, lossy]"
    return md


def md_to_confluence_storage(source: str) -> str:
    """Convert Markdown to Confluence Storage Format XHTML."""
    if not source:
        return ""
    result = cfxmark.to_cfx(source)
    return result.xhtml or ""


def _extract_name(value: Any) -> str:
    """Extract display name from a field that may be a string, dict, or None."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("display_name", value.get("displayName", value.get("name", "")))
    return str(value)


def format_md_issue(issue: dict[str, Any]) -> str:
    """Format a Jira issue dict as Markdown with description converted from wiki markup."""
    key = issue.get("key", "")
    summary = issue.get("summary", "")
    status = _extract_name(issue.get("status"))
    issuetype = _extract_name(issue.get("issuetype") or issue.get("issue_type") or issue.get("type"))
    priority = _extract_name(issue.get("priority"))
    assignee = _extract_name(issue.get("assignee"))
    description_raw = issue.get("description", "")
    description_md = jira_wiki_to_md(description_raw) if description_raw else ""

    lines = [
        f"# {key}: {summary}",
        "",
        f"**Type:** {issuetype}  **Status:** {status}  **Priority:** {priority}  **Assignee:** {assignee}",
    ]
    if description_md:
        lines += ["", "## Description", "", description_md]
    return "\n".join(lines)
