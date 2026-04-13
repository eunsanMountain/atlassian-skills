from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from atlassian_skills.confluence.models import ConfluenceSearchResult, Page
    from atlassian_skills.jira.models import Issue, SearchResult, Transition, WatcherList, WorklogList


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


def format_compact(data: Any) -> str:
    """Return a compact, pipe-separated string representation.

    Handles pydantic models (Issue, SearchResult, Page, ConfluenceSearchResult,
    Transition, WatcherList, WorklogList), lists of dicts, single dicts, strings,
    and arbitrary objects.
    """
    # Import here to avoid circular imports at module load time
    from atlassian_skills.confluence.models import ConfluenceSearchResult, Page
    from atlassian_skills.jira.models import Issue, SearchResult, Transition, WatcherList, WorklogList

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
    if isinstance(data, list):
        return "\n".join(_format_issue_row(item) if isinstance(item, dict) else str(item) for item in data)
    if isinstance(data, dict):
        return _format_issue_row(data)
    return str(data)
