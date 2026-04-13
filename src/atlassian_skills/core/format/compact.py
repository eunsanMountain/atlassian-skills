from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from atlassian_skills.confluence.models import (
        Attachment,
        Comment,
        ConfluenceSearchResult,
        Label,
        Page,
    )
    from atlassian_skills.jira.models import (
        Board,
        Issue,
        JiraAttachment,
        JiraComment,
        SearchResult,
        Sprint,
        Transition,
        WatcherList,
        WorklogList,
    )


def _format_issue_row(issue: dict[str, Any]) -> str:
    """Format a single Jira issue dict as a pipe-separated compact line.

    Expected keys (all optional, falls back to empty string):
        key, status, issuetype, priority, assignee, summary, updated
    """
    key = issue.get("key", "")
    status = issue.get("status", "")
    issuetype = issue.get("issuetype", issue.get("type", ""))
    priority = issue.get("priority", "")
    assignee = issue.get("assignee", "")
    summary = issue.get("summary", "")
    updated = issue.get("updated", "")
    return f"{key} | {status} | {issuetype} | {priority} | {assignee} | {summary} | {updated}"


def _format_jira_issue(issue: Issue) -> str:
    """Format a Jira Issue pydantic model as compact text."""
    status_name = issue.status.name if issue.status else ""
    issue_type = issue.issue_type.name if issue.issue_type else ""
    priority = issue.priority.name if issue.priority else ""
    assignee = issue.assignee.display_name if issue.assignee else ""
    project = issue.project.key if issue.project else ""
    labels = ",".join(issue.labels) if issue.labels else ""
    components = ",".join(c.name for c in issue.components) if issue.components else ""
    parts = [
        f"{issue.key} | {status_name} | {issue_type} | {priority} | {assignee} | {issue.summary}",
    ]
    if project:
        parts.append(f"project:{project}")
    if labels:
        parts.append(f"labels:{labels}")
    if components:
        parts.append(f"components:{components}")
    return "\n".join(parts)


def _format_jira_search_result(result: SearchResult) -> str:
    """Format a Jira SearchResult pydantic model as compact text."""
    header = f"total:{result.total} start:{result.start_at} max:{result.max_results}"
    rows = [_format_jira_issue(issue) for issue in result.issues]
    return header + "\n" + "\n---\n".join(rows)


def _format_confluence_page(page: Page) -> str:
    """Format a Confluence Page pydantic model as compact text (metadata only, no body)."""
    space_key = page.space.key if page.space else ""
    version = page.version if isinstance(page.version, int) else (page.version.number if page.version else "")
    parts = [f"{page.id} | {page.title} | {page.type} | space:{space_key} | v{version}"]
    if page.url:
        parts.append(f"url:{page.url}")
    return "\n".join(parts)


def _format_confluence_search_result(result: ConfluenceSearchResult) -> str:
    """Format a Confluence ConfluenceSearchResult pydantic model as compact text."""
    header = f"total:{result.total} start:{result.start} limit:{result.limit}"
    rows = [_format_confluence_page(page) for page in result.results]
    return header + "\n" + "\n---\n".join(rows)


def _format_transition(transition: Transition) -> str:
    """Format a Jira Transition as a compact one-liner."""
    to_status = transition.to_status.name if transition.to_status else ""
    return f"{transition.id} | {transition.name} | {to_status}"


def _format_watcher_list(watchers: WatcherList) -> str:
    """Format a Jira WatcherList as a compact one-liner."""
    key = watchers.issue_key or ""
    return f"{key}: {watchers.watcher_count} watchers"


def _format_worklog_list(worklogs: WorklogList) -> str:
    """Format a Jira WorklogList as compact lines (one per worklog)."""
    total = worklogs.total if worklogs.total is not None else len(worklogs.worklogs)
    lines = [f"{total} worklogs"]
    for w in worklogs.worklogs:
        author = w.author.display_name if w.author else ""
        started = w.started or w.created or ""
        time_spent = w.time_spent or ""
        lines.append(f"  {author} | {started} | {time_spent}")
    return "\n".join(lines)


def _format_board(board: Board) -> str:
    """Format a Jira Board as a compact one-liner."""
    return f"{board.id} | {board.name} | {board.type}"


def _format_sprint(sprint: Sprint) -> str:
    """Format a Jira Sprint as a compact one-liner."""
    state = sprint.state or ""
    start = sprint.start_date or ""
    end = sprint.end_date or ""
    return f"{sprint.id} | {sprint.name} | {state} | {start} | {end}"


def _format_jira_attachment(att: JiraAttachment) -> str:
    """Format a Jira Attachment as a compact one-liner."""
    author = att.author.display_name if att.author else ""
    created = att.created or ""
    size = att.size or 0
    mime = att.mime_type or ""
    return f"{att.id} | {att.filename} | {mime} | {size} | {author} | {created}"


def _format_jira_comment(comment: JiraComment) -> str:
    """Format a Jira Comment as compact text."""
    author = comment.author.display_name if comment.author else ""
    created = comment.created or ""
    body_preview = (comment.body or "")[:80].replace("\n", " ")
    return f"{comment.id} | {author} | {created} | {body_preview}"


def _format_confluence_label(label: Label) -> str:
    """Format a Confluence Label as a compact one-liner."""
    return f"{label.name} ({label.prefix})"


def _format_confluence_comment(comment: Comment) -> str:
    """Format a Confluence Comment as compact text."""
    author = comment.version.by.display_name if comment.version and comment.version.by else ""
    when = comment.version.when or "" if comment.version else ""
    body_preview = (comment.body_view or "")[:80].replace("\n", " ")
    return f"{comment.id} | {author} | {when} | {body_preview}"


def _format_confluence_attachment(attachment: Attachment) -> str:
    """Format a Confluence Attachment as a compact one-liner."""
    media = attachment.media_type or ""
    size = attachment.file_size or 0
    return f"{attachment.id} | {attachment.title} | {media} | {size}B"


def format_compact(data: Any) -> str:
    """Return a compact, pipe-separated string representation.

    Handles pydantic models (Issue, SearchResult, Page, ConfluenceSearchResult,
    Transition, WatcherList, WorklogList), lists of dicts, single dicts, strings,
    and arbitrary objects.
    """
    # Import here to avoid circular imports at module load time
    from atlassian_skills.confluence.models import (
        Attachment,
        Comment,
        ConfluenceSearchResult,
        Label,
        Page,
    )
    from atlassian_skills.jira.models import (
        Board,
        Issue,
        JiraAttachment,
        JiraComment,
        SearchResult,
        Sprint,
        Transition,
        WatcherList,
        WorklogList,
    )

    if isinstance(data, Issue):
        return _format_jira_issue(data)
    if isinstance(data, SearchResult):
        return _format_jira_search_result(data)
    if isinstance(data, Page):
        return _format_confluence_page(data)
    if isinstance(data, ConfluenceSearchResult):
        return _format_confluence_search_result(data)
    if isinstance(data, Transition):
        return _format_transition(data)
    if isinstance(data, WatcherList):
        return _format_watcher_list(data)
    if isinstance(data, WorklogList):
        return _format_worklog_list(data)
    if isinstance(data, Board):
        return _format_board(data)
    if isinstance(data, Sprint):
        return _format_sprint(data)
    if isinstance(data, JiraAttachment):
        return _format_jira_attachment(data)
    if isinstance(data, JiraComment):
        return _format_jira_comment(data)
    if isinstance(data, Label):
        return _format_confluence_label(data)
    if isinstance(data, Comment):
        return _format_confluence_comment(data)
    if isinstance(data, Attachment):
        return _format_confluence_attachment(data)
    if isinstance(data, list):
        return "\n".join(format_compact(item) for item in data)
    if isinstance(data, dict):
        return _format_issue_row(data)
    return str(data)
