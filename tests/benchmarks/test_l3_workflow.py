"""L3 token workflow benchmarks: end-to-end workflow must achieve >= 60% token reduction vs MCP."""
from __future__ import annotations

import json

import pytest

from atlassian_skills.core.format.compact import format_compact
from atlassian_skills.jira.models import Issue, Transition, WatcherList, WorklogList

from .scenarios import count_tokens, load_fixture

pytestmark = pytest.mark.benchmark


def test_l3_workflow_scenario_5() -> None:
    """L3: Issue get + transitions + watchers + worklog workflow achieves >= 60% token reduction vs MCP."""
    issue_fixture = load_fixture("jira/get-issue-rlm3.json")
    transitions_fixture = load_fixture("jira/get-transitions-rlm3.json")
    watchers_fixture = load_fixture("jira/get-watchers-rlm3.json")
    worklog_fixture = load_fixture("jira/get-worklog-rlm3.json")

    # MCP total tokens: sum of all tool responses as raw JSON
    mcp_total = sum(
        count_tokens(json.dumps(f, ensure_ascii=False))
        for f in [issue_fixture, transitions_fixture, watchers_fixture, worklog_fixture]
    )

    # atls side: parse and format compact for each
    parts: list[str] = []

    # Issue compact
    issue = Issue.model_validate(issue_fixture)
    parts.append(format_compact(issue))

    # Transitions compact
    if isinstance(transitions_fixture, list):
        transition_list = transitions_fixture
    else:
        transition_list = transitions_fixture.get("transitions", [])
    for t in transition_list:
        parts.append(format_compact(Transition.model_validate(t)))

    # Watchers compact
    watchers = WatcherList.model_validate(watchers_fixture)
    parts.append(format_compact(watchers))

    # Worklog compact
    worklogs = WorklogList.model_validate(worklog_fixture)
    parts.append(format_compact(worklogs))

    atls_total = count_tokens("\n".join(parts))
    reduction = 1.0 - (atls_total / mcp_total)

    print(
        f"\nScenario 5 - L3 Workflow: mcp={mcp_total} atls={atls_total} reduction={reduction:.1%}"
    )
    assert reduction >= 0.60, (
        f"L3 fail: {reduction:.1%} reduction < 60% target (mcp={mcp_total}, atls={atls_total})"
    )
