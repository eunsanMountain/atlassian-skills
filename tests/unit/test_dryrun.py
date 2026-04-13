from __future__ import annotations

import json

from atlassian_skills.core.dryrun import format_dry_run


class TestFormatDryRun:
    def test_compact_format(self) -> None:
        result = format_dry_run(
            method="POST",
            url="https://jira.corp.example.com/rest/api/2/issue",
            body={"summary": "Test issue"},
        )
        assert result.startswith("[DRY RUN] POST https://jira.corp.example.com/rest/api/2/issue")
        assert "Body:" in result
        assert "Test issue" in result

    def test_json_format(self) -> None:
        result = format_dry_run(
            method="PUT",
            url="https://jira.corp.example.com/rest/api/2/issue/PROJ-1",
            body={"status": "done"},
            fmt="json",
        )
        parsed = json.loads(result)
        assert parsed["dry_run"] is True
        assert parsed["method"] == "PUT"
        assert parsed["url"] == "https://jira.corp.example.com/rest/api/2/issue/PROJ-1"
        assert parsed["body"] == {"status": "done"}

    def test_auth_masking(self) -> None:
        result = format_dry_run(
            method="POST",
            url="https://jira.corp.example.com/rest/api/2/issue",
            headers={"Authorization": "Bearer mytoken123", "Content-Type": "application/json"},
        )
        assert "Bearer ***" in result
        assert "mytoken123" not in result
        assert "Content-Type: application/json" in result

    def test_auth_masking_json(self) -> None:
        result = format_dry_run(
            method="POST",
            url="https://jira.corp.example.com/rest/api/2/issue",
            headers={"Authorization": "Bearer mytoken123"},
            fmt="json",
        )
        parsed = json.loads(result)
        assert parsed["headers"]["Authorization"] == "Bearer ***"

    def test_long_body_truncation(self) -> None:
        long_body = "z" * 500
        result = format_dry_run(
            method="POST",
            url="https://jira.corp.example.com/rest/api/2/issue",
            body=long_body,
        )
        assert "... (500 chars)" in result
        # Only 200 chars of the body should appear in the output
        assert result.count("z") == 200
