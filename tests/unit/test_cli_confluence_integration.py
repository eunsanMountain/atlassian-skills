from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from atlassian_skills.cli.main import app
from atlassian_skills.confluence.stateless_write import build_source_conversion
from atlassian_skills.core.config import Config
from atlassian_skills.core.errors import ExitCode

FIXTURES = Path(__file__).parent.parent / "fixtures" / "confluence"
CONFLUENCE_URL = "https://confluence.example.com"
runner = CliRunner()

# Raw Confluence REST API format for a Page (what the server actually returns)
_RAW_PAGE = {
    "id": "12345678",
    "title": "[PROJ-3] 검색 결과 정렬 개선",
    "type": "page",
    "status": "current",
    "space": {"key": "TESTSPACE", "name": "Test Lab", "type": "global"},
    "version": {"number": 2, "when": "2024-01-01T00:00:00.000Z"},
    "_links": {"webui": "/pages/viewpage.action?pageId=12345678"},
    "body": {
        "storage": {
            "value": "<p>Test content</p>",
            "representation": "storage",
        }
    },
}

_RAW_PAGE_CREATED = {
    "id": "123456789",
    "title": "Test Page",
    "type": "page",
    "status": "current",
    "space": {"key": "TESTSPACE", "name": "Test Lab", "type": "global"},
    "version": {"number": 1},
    "_links": {"webui": "/pages/viewpage.action?pageId=123456789"},
}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject URL + token for 'default' profile and isolate from real config file."""
    monkeypatch.setenv("ATLS_DEFAULT_CONFLUENCE_URL", CONFLUENCE_URL)
    monkeypatch.setenv("ATLS_DEFAULT_CONFLUENCE_TOKEN", "test-token")
    # Prevent the real ~/.config/atlassian-skills/config.toml from overriding URLs
    monkeypatch.setattr(
        "atlassian_skills.cli.confluence.load_config",
        lambda: Config(),
    )


# ---------------------------------------------------------------------------
# Read commands
# ---------------------------------------------------------------------------


@respx.mock
def test_cli_confluence_page_get_compact() -> None:
    """atls confluence page get <id> returns exit 0 and shows page title."""
    # Client appends ?expand=... so match by URL regex to handle query params
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=_RAW_PAGE)
    )
    result = runner.invoke(app, ["confluence", "page", "get", "12345678"])
    assert result.exit_code == 0, result.output


@respx.mock
def test_cli_confluence_page_get_json_format() -> None:
    """--format json on page get returns valid JSON with id."""
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=_RAW_PAGE)
    )
    result = runner.invoke(app, ["--format", "json", "confluence", "page", "get", "12345678"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["id"] == "12345678"


@respx.mock
def test_cli_confluence_page_get_body_repr_md() -> None:
    """--body-repr md fetches full page with body expansion and returns exit 0."""
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=_RAW_PAGE)
    )
    result = runner.invoke(
        app,
        ["confluence", "page", "get", "12345678", "--body-repr", "md", "--format", "md"],
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == "Test content\n"
    assert "<!-- atls:" not in result.stdout
    assert "# conversion: push_safe=false" in result.stderr


@respx.mock
def test_cli_confluence_page_get_md_passthrough_is_canonical_and_content_only() -> None:
    page = dict(_RAW_PAGE)
    page["body"] = {
        "storage": {
            "value": "<!-- custom:keep --><p>Body</p>",
            "representation": "storage",
        }
    }
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=page)
    )

    result = runner.invoke(
        app,
        [
            "--format",
            "json",
            "confluence",
            "page",
            "get",
            "12345678",
            "--body-repr",
            "md",
            "--passthrough-prefix",
            "zeta:",
            "--passthrough-prefix",
            "custom:",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["conversion_options"]["passthrough_prefixes"] == ["custom:", "zeta:"]
    assert "atls:managed" not in payload["body_storage"]


@pytest.mark.parametrize("body_repr", ["raw", "storage", "view"])
def test_cli_confluence_page_get_rejects_passthrough_without_markdown_conversion(body_repr: str) -> None:
    result = runner.invoke(
        app,
        [
            "--format",
            "json",
            "confluence",
            "page",
            "get",
            "12345678",
            "--body-repr",
            body_repr,
            "--passthrough-prefix",
            "custom:",
        ],
    )

    assert result.exit_code == ExitCode.VALIDATION, result.output
    assert json.loads(result.output)["error"]["context"]["reason"] == "passthrough_requires_markdown_conversion"


@respx.mock
def test_cli_confluence_page_get_view_raw_is_exact_server_rendered_html() -> None:
    rendered = '<div class="content"><p>Rendered</p></div>'
    page = {**_RAW_PAGE, "body": {"view": {"value": rendered, "representation": "view"}}}
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=page)
    )

    result = runner.invoke(
        app,
        ["confluence", "page", "get", "12345678", "--body-repr", "view", "--format", "raw"],
    )

    assert result.exit_code == 0, result.output
    assert result.output == rendered


@respx.mock
def test_cli_confluence_page_get_view_json_marks_body_non_publishable() -> None:
    page = {**_RAW_PAGE, "body": {"view": {"value": "<p>Rendered</p>", "representation": "view"}}}
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=page)
    )

    result = runner.invoke(
        app,
        ["--format", "json", "confluence", "page", "get", "12345678", "--body-repr", "view"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["representation"] == "view"
    assert payload["editable"] is False
    assert payload["publishable"] is False
    assert payload["reason"] == "server-rendered-html"


@respx.mock
def test_cli_confluence_page_inspect_append_recommends_dry_run_proof() -> None:
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678(?:\?|$)").mock(
        return_value=httpx.Response(200, json=_RAW_PAGE)
    )
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678/child/attachment").mock(
        return_value=httpx.Response(200, json={"results": [], "size": 0})
    )

    result = runner.invoke(
        app,
        [
            "--format",
            "json",
            "confluence",
            "page",
            "inspect",
            "12345678",
            "--intent",
            "append",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["recommended_workflow"] == "pull-md"
    assert payload["preferred_proof"] == "exact_remote_prefix_append"
    assert payload["managed_artifact_created"] is False


@respx.mock
def test_cli_confluence_page_get_md_has_no_control_marker() -> None:
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=_RAW_PAGE)
    )

    result = runner.invoke(app, ["--format", "md", "confluence", "page", "get", "12345678"])

    assert result.exit_code == 0, result.output
    assert result.output.startswith("# [PROJ-3]")
    assert "<!-- atls:" not in result.output
    assert "# conversion: push_safe=false" in result.stderr


@pytest.mark.parametrize(
    ("format_args", "expects_json"),
    [
        (["--body-repr", "md", "--format", "json"], True),
        (["--body-repr", "md", "--format", "md"], False),
    ],
)
@respx.mock
def test_cli_confluence_readable_markdown_reports_omitted_table_backgrounds(
    format_args: list[str],
    expects_json: bool,
) -> None:
    colored_page = dict(_RAW_PAGE)
    colored_page["body"] = {
        "storage": {
            "value": (
                '<table><tbody><tr><th data-highlight-colour="#fff0b3">Heading</th>'
                '<td style="background-color: #deebff">Value</td><td>Plain</td></tr></tbody></table>'
            ),
            "representation": "storage",
        }
    }
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=colored_page)
    )

    result = runner.invoke(app, ["confluence", "page", "get", "12345678", *format_args])

    assert result.exit_code == 0, result.output
    if expects_json:
        payload = json.loads(result.stdout)
        assert payload["conversion"]["diagnostics"] == [
            {
                "code": "table-cell-background-omitted",
                "severity": "warning",
                "count": 2,
                "message": (
                    "Readable Markdown omits the backgrounds of 2 table cells; "
                    "the remote page remains the presentation source of truth."
                ),
            }
        ]
        assert result.stderr == ""
    else:
        assert "data-highlight-colour" not in result.stdout
        assert result.stderr.count("table-cell-background-omitted") == 1
        assert "count=2" in result.stderr


@respx.mock
def test_cli_confluence_readable_markdown_has_no_background_warning_when_none_are_omitted() -> None:
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=_RAW_PAGE)
    )

    result = runner.invoke(
        app,
        ["confluence", "page", "get", "12345678", "--body-repr", "md", "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "diagnostics" not in payload["conversion"]
    assert result.stderr == ""


@respx.mock
def test_cli_confluence_empty_page_get_md_is_content_only_and_not_json() -> None:
    empty_page = dict(_RAW_PAGE)
    empty_page["body"] = {"storage": {"value": "", "representation": "storage"}}
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=empty_page)
    )

    result = runner.invoke(app, ["confluence", "page", "get", "12345678", "--format", "md"])

    assert result.exit_code == 0, result.output
    assert result.output.startswith("# [PROJ-3]")
    assert "<!-- atls:" not in result.output
    assert not result.output.lstrip().startswith("{")
    assert "# conversion: push_safe=false" in result.stderr


@respx.mock
def test_cli_confluence_page_history_md_emits_conversion_diagnostics() -> None:
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=_RAW_PAGE)
    )

    result = runner.invoke(
        app,
        ["confluence", "page", "history", "12345678", "2", "--format", "md"],
    )

    assert result.exit_code == 0, result.output
    assert result.output.startswith("# [PROJ-3]")
    assert "<!-- atls:" not in result.output
    assert "# conversion: push_safe=false" in result.stderr


@respx.mock
def test_cli_confluence_page_get_not_found() -> None:
    """404 on page get exits with NOT_FOUND (2)."""
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/999999").mock(
        return_value=httpx.Response(404, json={"message": "Not found"})
    )
    result = runner.invoke(app, ["confluence", "page", "get", "999999"])
    assert result.exit_code == ExitCode.NOT_FOUND


@respx.mock
def test_cli_confluence_page_search() -> None:
    """atls confluence page search <cql> returns exit 0."""
    search_response = {
        "results": [_RAW_PAGE],
        "start": 0,
        "limit": 25,
        "size": 1,
        "totalSize": 1,
        "_links": {},
    }
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/search").mock(
        return_value=httpx.Response(200, json=search_response)
    )
    result = runner.invoke(app, ["confluence", "page", "search", "space=TESTSPACE"])
    assert result.exit_code == 0, result.output


@respx.mock
def test_cli_confluence_page_search_local_json_format() -> None:
    """Local --format json on page search works after the subcommand."""
    search_response = {
        "results": [_RAW_PAGE],
        "start": 0,
        "limit": 25,
        "size": 1,
        "totalSize": 1,
        "_links": {},
    }
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/search").mock(
        return_value=httpx.Response(200, json=search_response)
    )
    result = runner.invoke(app, ["confluence", "page", "search", "space=TESTSPACE", "--format", "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data[0]["id"] == "12345678"


@respx.mock
def test_cli_confluence_comments_list() -> None:
    """atls confluence comment list <page_id> returns exit 0."""
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678/child/comment").mock(
        return_value=httpx.Response(200, json=_load("get-comments-sample.json"))
    )
    result = runner.invoke(app, ["confluence", "comment", "list", "12345678"])
    assert result.exit_code == 0, result.output


@respx.mock
def test_cli_confluence_labels_list() -> None:
    """atls confluence label list <page_id> returns exit 0 and includes label data."""
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678/label").mock(
        return_value=httpx.Response(200, json=_load("get-labels-sample.json"))
    )
    result = runner.invoke(app, ["--format", "json", "confluence", "label", "list", "12345678"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    names = [item.get("name") for item in data]
    assert "architecture" in names


# ---------------------------------------------------------------------------
# Write commands
# ---------------------------------------------------------------------------


@respx.mock
def test_cli_confluence_page_create_dry_run(tmp_path: Path) -> None:
    """--dry-run on page create shows POST preview without hitting API."""
    body_file = tmp_path / "body.html"
    body_file.write_text("<p>Hello world</p>")
    result = runner.invoke(
        app,
        [
            "confluence",
            "page",
            "create",
            "--space",
            "TESTSPACE",
            "--title",
            "Test Page",
            "--body-file",
            str(body_file),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "POST" in result.output
    assert "TESTSPACE" in result.output


@respx.mock
def test_cli_confluence_page_create_success(tmp_path: Path) -> None:
    """page create POSTs and returns created page id."""
    body_file = tmp_path / "body.html"
    body_file.write_text("<p>Hello world</p>")
    created = {
        **_RAW_PAGE_CREATED,
        "ancestors": [],
        "body": {"storage": {"value": "<p>Hello world</p>", "representation": "storage"}},
    }
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/search").mock(
        return_value=httpx.Response(200, json={"results": [], "size": 0})
    )
    respx.post(f"{CONFLUENCE_URL}/rest/api/content").mock(return_value=httpx.Response(200, json=_RAW_PAGE_CREATED))
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/123456789").mock(
        return_value=httpx.Response(200, json=created)
    )
    result = runner.invoke(
        app,
        [
            "--format",
            "json",
            "confluence",
            "page",
            "create",
            "--space",
            "TESTSPACE",
            "--title",
            "Test Page",
            "--body-file",
            str(body_file),
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["id"] == "123456789"


@respx.mock
def test_cli_confluence_page_create_json_includes_write_conversion_warnings(tmp_path: Path) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_bytes(b"\xef\xbb\xbf# Heading\r\n\r\n```text\r\nBody\r\n```\r\n")
    candidate = build_source_conversion(body_file.read_bytes().decode("utf-8-sig")).candidate_storage
    created = {
        **_RAW_PAGE_CREATED,
        "ancestors": [],
        "body": {"storage": {"value": candidate, "representation": "storage"}},
    }
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/search").mock(
        return_value=httpx.Response(200, json={"results": [], "size": 0})
    )
    respx.post(f"{CONFLUENCE_URL}/rest/api/content").mock(return_value=httpx.Response(200, json=_RAW_PAGE_CREATED))
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/123456789").mock(
        return_value=httpx.Response(200, json=created)
    )

    result = runner.invoke(
        app,
        [
            "--format",
            "json",
            "confluence",
            "page",
            "create",
            "--space",
            "TESTSPACE",
            "--title",
            "Test Page",
            "--body-file",
            str(body_file),
            "--body-format",
            "md",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["conversion"]["push_safe"] is True
    assert any("normalized to LF" in warning for warning in data["conversion"]["warnings"])


def test_cli_confluence_page_create_rejects_readable_markdown(tmp_path: Path) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text("<!-- atls:mode=readable push=blocked -->\n\n# Read only\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "confluence",
            "page",
            "create",
            "--space",
            "SPACE",
            "--title",
            "Read only",
            "--body-file",
            str(body_file),
            "--body-format",
            "md",
            "--dry-run",
        ],
    )

    assert result.exit_code == 7
    assert "generated for reading only" in result.output


@respx.mock
def test_cli_confluence_page_create_loss_requires_returned_conversion_fingerprint(tmp_path: Path) -> None:
    body_file = tmp_path / "lossy.md"
    body_file.write_text("x <span>raw</span> y\n", encoding="utf-8")
    post = respx.post(f"{CONFLUENCE_URL}/rest/api/content").mock(
        return_value=httpx.Response(500, json={"message": "must not write"})
    )

    dry_run = runner.invoke(
        app,
        [
            "--format",
            "json",
            "confluence",
            "page",
            "create",
            "--space",
            "TESTSPACE",
            "--title",
            "User supplied title",
            "--body-file",
            str(body_file),
            "--body-format",
            "md",
            "--dry-run",
        ],
    )
    assert dry_run.exit_code == 0, dry_run.output
    dry_payload = json.loads(dry_run.output)
    assert dry_payload["status"] == "conversion_consent_required"
    assert dry_payload["conversion_fingerprint"].startswith("conv_sha256:")
    dry_action = dry_payload["next_actions"][0]
    assert dry_action["id"] == "retry_with_consent"
    assert dry_action["requires_user_approval"] is True
    assert dry_action["description_code"] == "REVIEW_CONVERSION_AND_RETRY"
    assert dry_action["argv"][-2:] == ["--accept-conversion", dry_payload["conversion_fingerprint"]]

    actual = runner.invoke(
        app,
        [
            "--format",
            "json",
            "confluence",
            "page",
            "create",
            "--space",
            "TESTSPACE",
            "--title",
            "User supplied title",
            "--body-file",
            str(body_file),
            "--body-format",
            "md",
        ],
    )
    assert actual.exit_code == ExitCode.VALIDATION, actual.output
    error = json.loads(actual.output)["error"]
    assert error["code"] == "CONVERSION_CONSENT_REQUIRED"
    context = error["context"]
    assert context["reason"] == "conversion_consent_required"
    assert context["next_actions"] == [dry_action]
    assert post.called is False

    human = runner.invoke(
        app,
        [
            "confluence",
            "page",
            "create",
            "--space",
            "TESTSPACE",
            "--title",
            "User supplied title",
            "--body-file",
            str(body_file),
            "--body-format",
            "md",
        ],
    )
    assert human.exit_code == ExitCode.VALIDATION, human.output
    summary_at = human.output.index("Loss summary:")
    alternative_at = human.output.index("Alternative:")
    retry_at = human.output.index("Retry:")
    assert summary_at < alternative_at < retry_at
    assert "\n\nRetry:" in human.output
    assert "--accept-conversion conv_sha256:" in human.output
    assert human.output.rstrip().splitlines()[-1].startswith("Retry: atls confluence page create ")
    assert post.called is False


@respx.mock
def test_cli_confluence_page_update(tmp_path: Path) -> None:
    """page update fetches current version then PUTs new content."""
    body_file = tmp_path / "body.html"
    body_file.write_text("<p>Updated content</p>")
    # GET is called with query params by the client
    updated = {
        **_RAW_PAGE,
        "version": {"number": 3},
        "body": {"storage": {"value": "<p>Updated content</p>", "representation": "storage"}},
    }
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        side_effect=[
            httpx.Response(200, json=_RAW_PAGE),
            httpx.Response(200, json=_RAW_PAGE),
            httpx.Response(200, json=updated),
        ]
    )
    respx.put(f"{CONFLUENCE_URL}/rest/api/content/12345678").mock(return_value=httpx.Response(200, json=_RAW_PAGE))
    result = runner.invoke(
        app,
        ["confluence", "page", "update", "12345678", "--body-file", str(body_file)],
    )
    assert result.exit_code == 0, result.output


@respx.mock
def test_cli_confluence_page_update_dry_run(tmp_path: Path) -> None:
    """--dry-run on page update fetches page then shows PUT preview."""
    body_file = tmp_path / "body.html"
    body_file.write_text("<p>Draft content</p>")
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=_RAW_PAGE)
    )
    result = runner.invoke(
        app,
        ["confluence", "page", "update", "12345678", "--body-file", str(body_file), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "PUT" in result.output


@respx.mock
def test_cli_confluence_page_update_md_loss_is_read_only_without_exact_consent(tmp_path: Path) -> None:
    body_file = tmp_path / "lossy.md"
    body_file.write_text("x <span>raw</span> y\n", encoding="utf-8")
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=_RAW_PAGE)
    )
    put = respx.put(f"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(500, json={"message": "must not write"})
    )

    result = runner.invoke(
        app,
        [
            "--format",
            "json",
            "confluence",
            "page",
            "update",
            "12345678",
            "--body-file",
            str(body_file),
            "--body-format",
            "md",
            "--if-version",
            "2",
        ],
    )

    assert result.exit_code == ExitCode.VALIDATION, result.output
    error = json.loads(result.output)["error"]
    assert error["code"] == "MIGRATION_CONSENT_REQUIRED"
    context = error["context"]
    assert context["reason"] == "migration_consent_required"
    assert context["migration_fingerprint"].startswith("mig_sha256:")
    assert context["source_conversion_report"]["occurrences"]
    action = context["next_actions"][0]
    assert action["id"] == "retry_with_consent"
    assert action["requires_user_approval"] is True
    assert action["description_code"] == "REVIEW_MIGRATION_AND_RETRY"
    assert action["argv"][-2:] == ["--accept-migration", context["migration_fingerprint"]]
    assert put.called is False

    human = runner.invoke(
        app,
        [
            "confluence",
            "page",
            "update",
            "12345678",
            "--body-file",
            str(body_file),
            "--body-format",
            "md",
            "--if-version",
            "2",
        ],
    )
    assert human.exit_code == ExitCode.VALIDATION, human.output
    summary_at = human.output.index("Loss summary:")
    alternative_at = human.output.index("Alternative:")
    retry_at = human.output.index("Retry:")
    assert summary_at < alternative_at < retry_at
    assert "\n\nRetry:" in human.output
    assert "--accept-migration mig_sha256:" in human.output
    assert human.output.rstrip().splitlines()[-1].startswith("Retry: atls confluence page update 12345678 ")
    assert str(_RAW_PAGE["title"]) not in human.output
    assert CONFLUENCE_URL not in human.output
    assert put.called is False


@respx.mock
def test_cli_confluence_page_delete() -> None:
    """page delete DELETEs and exits 0."""
    respx.delete(f"{CONFLUENCE_URL}/rest/api/content/12345678").mock(return_value=httpx.Response(204))
    result = runner.invoke(app, ["confluence", "page", "delete", "12345678"])
    assert result.exit_code == 0, result.output


@respx.mock
def test_cli_confluence_page_delete_json_format() -> None:
    """page delete with --format json outputs deleted id."""
    respx.delete(f"{CONFLUENCE_URL}/rest/api/content/12345678").mock(return_value=httpx.Response(204))
    result = runner.invoke(app, ["--format", "json", "confluence", "page", "delete", "12345678"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["deleted"] == "12345678"


@respx.mock
def test_cli_confluence_page_delete_dry_run() -> None:
    """--dry-run on page delete shows DELETE preview without hitting API."""
    result = runner.invoke(app, ["confluence", "page", "delete", "12345678", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "DELETE" in result.output
    assert "12345678" in result.output


@respx.mock
def test_cli_confluence_page_push_md_rejects_stdin_without_binding_identity(tmp_path: Path) -> None:
    """Managed push rejects stdin before any page read."""
    current_page = dict(_RAW_PAGE)
    current_page["body"] = {"storage": {"value": "<p>Old content</p>", "representation": "storage"}}
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=current_page)
    )
    result = runner.invoke(
        app,
        [
            "--quiet",
            "confluence",
            "page",
            "push-md",
            "12345678",
            "--md-file",
            "-",
            "--dry-run",
            "--format",
            "json",
        ],
        input="# Updated from stdin\n",
    )
    assert result.exit_code == ExitCode.VALIDATION, result.output
    data = json.loads(result.output)
    assert data["error"]["code"] == "VALIDATION"
    assert data["error"]["context"]["reason"] == "managed_file_required"


def test_cli_confluence_page_push_md_missing_file_is_json_not_found(tmp_path: Path) -> None:
    missing = tmp_path / "missing.md"

    result = runner.invoke(
        app,
        [
            "confluence",
            "page",
            "push-md",
            "12345678",
            "--md-file",
            str(missing),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == ExitCode.NOT_FOUND
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "NOT_FOUND"
    assert payload["error"]["context"] == {"reason": "managed_file_not_found", "path": str(missing)}


def test_cli_confluence_page_push_md_rejects_readable_input_before_network() -> None:
    result = runner.invoke(
        app,
        [
            "--format",
            "json",
            "confluence",
            "page",
            "push-md",
            "12345678",
            "--md-file",
            "-",
        ],
        input="<!-- atls:mode=readable push=blocked -->\n\n# Read only\n",
    )

    assert result.exit_code == ExitCode.VALIDATION
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "VALIDATION"


@respx.mock
def test_cli_confluence_diff_local_json_reports_conversion_safety(tmp_path: Path) -> None:
    current_page = dict(_RAW_PAGE)
    current_page["body"] = {
        "storage": {
            "value": ('<ac:structured-macro xmlns:ac="http://atlassian.com/content" ac:name="sample-unknown"/>'),
            "representation": "storage",
        }
    }
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=current_page)
    )
    local_file = tmp_path / "page.md"
    local_file.write_text("# Replacement\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "--format",
            "json",
            "confluence",
            "page",
            "diff-local",
            "12345678",
            str(local_file),
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["conversion"]["push_safe"] is False
    assert any("server:" in loss for loss in payload["conversion"]["losses"])


def test_cli_confluence_page_pull_batch_uses_one_client_and_formats_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atlassian_skills.confluence.pull_md import PullPageResult

    client = object()
    monkeypatch.setattr("atlassian_skills.cli.confluence._make_client", lambda _ctx: client)
    calls: list[tuple[object, list[str], Path]] = []

    def fake_pull(
        selected_client: object, page_ids: list[str], output_root: Path, **_kwargs: object
    ) -> list[PullPageResult]:
        calls.append((selected_client, page_ids, output_root))
        return [PullPageResult("100", "Page One", output_root / "Page One--100" / "Page One.md", 3, 10)]

    monkeypatch.setattr("atlassian_skills.confluence.pull_md.pull_pages_batch", fake_pull)
    result = runner.invoke(
        app,
        [
            "--format",
            "json",
            "confluence",
            "page",
            "pull-batch",
            "100",
            "200",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [(client, ["100", "200"], tmp_path)]
    payload = json.loads(result.output)
    assert payload[0]["page_id"] == "100"
    assert payload[0]["assets"] == 10


def test_cli_confluence_page_pull_batch_does_not_emit_legacy_state_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atlassian_skills.confluence.pull_md import PullPageResult

    monkeypatch.setattr("atlassian_skills.cli.confluence._make_client", lambda _ctx: object())
    managed = tmp_path / "Page One--100" / "Page One.md"
    monkeypatch.setattr(
        "atlassian_skills.confluence.pull_md.pull_pages_batch",
        lambda *_args, **_kwargs: [
            PullPageResult(
                "100",
                "Page One",
                managed,
                3,
                0,
                migration_report={"schema": "cfxmark-migration-report-v1", "occurrences": []},
                migration_report_sha256="sha256:" + "a" * 64,
            )
        ],
    )

    result = runner.invoke(
        app,
        ["confluence", "page", "pull-batch", "100", "--output-dir", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert "atls state" not in result.output
    assert "binding" not in result.stderr.lower()


# ---------------------------------------------------------------------------
# Exit code matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "http_status,expected_exit",
    [
        (401, ExitCode.AUTH),
        (403, ExitCode.PERMISSION),
        (404, ExitCode.NOT_FOUND),
        (500, ExitCode.NETWORK),
    ],
)
@respx.mock
def test_cli_confluence_exit_codes(http_status: int, expected_exit: int) -> None:
    """HTTP status codes map to the correct CLI exit codes."""
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(http_status, json={"message": "error"})
    )
    result = runner.invoke(app, ["confluence", "page", "get", "12345678"])
    assert result.exit_code == expected_exit


# ---------------------------------------------------------------------------
# Story 4.5: patch-text diagnostics through the real CLI entry point
#
# The unit tests cover classification; these cover what a person or an agent
# actually sees. A correct reason that never reaches the terminal is not a
# usable diagnostic.
# ---------------------------------------------------------------------------

_RAW_PAGE_INLINE_MARKUP = {
    "id": "12345678",
    "title": "Inline markup page",
    "type": "page",
    "status": "current",
    "space": {"key": "TESTSPACE", "name": "Test Lab", "type": "global"},
    "version": {"number": 2, "when": "2024-01-01T00:00:00.000Z"},
    "_links": {"webui": "/pages/viewpage.action?pageId=12345678"},
    "body": {"storage": {"value": "<p><strong>Important</strong> notice</p>", "representation": "storage"}},
}


@respx.mock
def test_cli_patch_text_boundary_failure_is_actionable_for_humans() -> None:
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=_RAW_PAGE_INLINE_MARKUP)
    )
    result = runner.invoke(
        app,
        [
            "confluence",
            "page",
            "patch-text",
            "12345678",
            "--find",
            "Important notice",
            "--replace",
            "Critical notice",
            "--if-version",
            "2",
            "--dry-run",
        ],
    )

    assert result.exit_code == ExitCode.VALIDATION, result.output
    # The counts that distinguish "no match" from "several matches" must survive
    # into the human envelope, not only into --format=json.
    assert "0 matches" in result.output
    assert "1 spanning inline markup" in result.output
    assert "spans inline markup" in result.output
    assert "Retry --find with the exact text of a single plain-text part." in result.output


def test_cli_patch_text_requires_source_version_before_remote_read() -> None:
    result = runner.invoke(
        app,
        [
            "--format",
            "json",
            "confluence",
            "page",
            "patch-text",
            "12345678",
            "--find",
            "Alpha",
            "--replace",
            "Beta",
            "--dry-run",
        ],
    )

    assert result.exit_code == ExitCode.VALIDATION, result.output
    assert json.loads(result.output)["error"]["context"]["reason"] == "patch_version_required"


@respx.mock
def test_cli_patch_text_boundary_failure_json_envelope() -> None:
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=_RAW_PAGE_INLINE_MARKUP)
    )
    result = runner.invoke(
        app,
        [
            "--format",
            "json",
            "confluence",
            "page",
            "patch-text",
            "12345678",
            "--find",
            "Important notice",
            "--replace",
            "Critical notice",
            "--if-version",
            "2",
            "--dry-run",
        ],
    )

    assert result.exit_code == ExitCode.VALIDATION, result.output
    context = json.loads(result.output)["error"]["context"]
    assert context["reason"] == "cross_text_node_boundary"
    assert context["patchable"] is False
    assert context["match_count"] == 0
    assert context["boundary_match_count"] == 1
    assert context["hint_code"] == "use_single_plain_text_leaf"
    assert [action["id"] for action in context["next_actions"]] == ["retry_inner_plain_text", "use_pull_md"]
    for action in context["next_actions"]:
        assert "argv" not in action


@respx.mock
def test_cli_patch_text_retrying_inside_one_leaf_succeeds() -> None:
    """The hint has to lead somewhere: the narrower --find must actually work."""
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=_RAW_PAGE_INLINE_MARKUP)
    )
    result = runner.invoke(
        app,
        [
            "--format",
            "json",
            "confluence",
            "page",
            "patch-text",
            "12345678",
            "--find",
            "Important",
            "--replace",
            "Critical",
            "--if-version",
            "2",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["patchable"] is True
    assert payload["put_count"] == 0


@respx.mock
def test_cli_patch_text_absent_text_is_not_reported_as_ambiguous() -> None:
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=_RAW_PAGE_INLINE_MARKUP)
    )
    result = runner.invoke(
        app,
        [
            "--format",
            "json",
            "confluence",
            "page",
            "patch-text",
            "12345678",
            "--find",
            "Nonexistent phrase",
            "--replace",
            "X",
            "--if-version",
            "2",
            "--dry-run",
        ],
    )

    assert result.exit_code == ExitCode.VALIDATION, result.output
    context = json.loads(result.output)["error"]["context"]
    assert context["reason"] == "text_not_found"
    # Regression: this used to be reported as text_occurrence_not_unique with a
    # match_count of 0, telling the caller to disambiguate nothing.
    assert context["match_count"] == 0
    assert [action["id"] for action in context["next_actions"]] == ["retry_inner_plain_text"]


# ---------------------------------------------------------------------------
# Read projection: does the reader learn the Markdown is not all of the page
# ---------------------------------------------------------------------------
#
# The unit tests decide the verdict. These pin that it survives the command --
# a correct verdict that never reaches stdout or stderr changes nothing about
# what an agent does next, which is the only reason the check was written.


_LOSSY_STORAGE = (
    "<p>visible intro</p>"
    "<table><tbody><tr><td>outer cell</td><td>"
    '<ac:structured-macro ac:name="expand"><ac:rich-text-body><p>중요 경고</p>'
    "</ac:rich-text-body></ac:structured-macro></td></tr></tbody></table>"
)


def _page_with(storage: str) -> dict:
    page = json.loads(json.dumps(_RAW_PAGE))
    page["body"]["storage"]["value"] = storage
    return page


@respx.mock
def test_page_get_md_json_says_the_projection_is_incomplete() -> None:
    """The machine-readable half. A caller reading the JSON gets the verdict, the
    element that went missing and where it was."""

    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=_page_with(_LOSSY_STORAGE))
    )
    result = runner.invoke(app, ["--format", "json", "confluence", "page", "get", "12345678", "--body-repr", "md"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["content_complete"] is False
    assert payload["attention_required"] is True
    assert payload["attention_reason"] == "content_incomplete"
    assert [item["code"] for item in payload["omissions"]] == ["element_body_omitted"]
    # And the way out, ready to run rather than described.
    argvs = [action["argv"] for action in payload["next_actions"]]
    assert ["confluence", "page", "get", "12345678", "--body-repr=storage", "--format=raw"] in argvs


@respx.mock
def test_page_get_md_warns_on_stderr_without_json() -> None:
    """The half a person sees. `--body-repr=md` prints the Markdown on stdout and
    nothing else, so the warning has to go to stderr or it corrupts the output
    it is warning about."""

    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=_page_with(_LOSSY_STORAGE))
    )
    result = runner.invoke(app, ["confluence", "page", "get", "12345678", "--body-repr", "md"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "content_incomplete" in result.stderr
    assert "do not summarize this page from this output alone" in result.stderr
    # One command, not a menu. `view` is the page as a person sees it, which is
    # what a summary is about; the JSON carries `storage` as well for a caller
    # that needs the exact markup.
    assert "next: atls confluence page get 12345678 --body-repr=view" in result.stderr
    assert "중요 경고" not in result.stdout


@respx.mock
def test_a_faithful_page_gets_no_warning_at_all() -> None:
    """The reason the warning is worth reading when it does appear."""

    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=_page_with("<p>alpha bravo charlie</p>"))
    )
    result = runner.invoke(app, ["confluence", "page", "get", "12345678", "--body-repr", "md"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "content_incomplete" not in result.stderr
    assert "structure_incomplete" not in result.stderr


def _page_with_storage(storage: str) -> dict[str, object]:
    return {**_RAW_PAGE, "body": {"storage": {"value": storage, "representation": "storage"}}}


def _full_replacement_invoke(tmp_path: Path, storage: str, *, fmt: str | None = None) -> object:
    """Drive `page update` into the explicit full-replacement gate.

    `B changed\n\nA\n` against `<p>A</p><p>B</p>` reorders and rewrites at once, which
    is what the strict source-bound proof cannot attribute -- the same shape the unit
    tests in `test_stateless_page_write.py` use, driven here through the real CLI so the
    console rendering is exercised too.
    """

    body_file = tmp_path / "candidate.md"
    body_file.write_text("B changed\n\nA\n", encoding="utf-8")
    respx.get(url__regex=rf"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(200, json=_page_with_storage(storage))
    )
    put = respx.put(f"{CONFLUENCE_URL}/rest/api/content/12345678").mock(
        return_value=httpx.Response(500, json={"message": "must not write"})
    )
    argv = ["confluence", "page", "update", "12345678", "--body-file", str(body_file), "--body-format", "md"]
    result = runner.invoke(app, ([*["--format", fmt], *argv] if fmt else argv))
    return result, put


@respx.mock
def test_full_replacement_consent_prints_the_command_its_hint_promises(tmp_path: Path) -> None:
    """The hint says "run the returned command"; the console has to return one.

    Every ingredient existed -- the raise site builds a `retry_with_consent` action whose
    argv is the exact retry, and `_handle_error` knows how to print one -- but
    `_CONSENT_ACTIONS` had no entry for `REVIEW_FULL_REPLACEMENT_AND_RETRY`, so the lookup
    returned None and the command was silently dropped. A user following the hint had
    nothing to follow, and the JSON envelope was the only way to recover the fingerprint.
    """

    result, put = _full_replacement_invoke(tmp_path, "<p>A</p><p>B</p>")

    assert result.exit_code == ExitCode.VALIDATION, result.output
    summary_at = result.output.index("Loss summary:")
    alternative_at = result.output.index("Alternative:")
    retry_at = result.output.index("Retry:")
    assert summary_at < alternative_at < retry_at
    assert "--accept-full-replacement repl_sha256:" in result.output
    assert result.output.rstrip().splitlines()[-1].startswith("Retry: atls confluence page update 12345678 ")
    assert str(_RAW_PAGE["title"]) not in result.output
    assert CONFLUENCE_URL not in result.output
    assert put.called is False


@respx.mock
def test_full_replacement_retry_carries_both_approvals_when_identities_are_discarded(tmp_path: Path) -> None:
    """The two-flag shape has to survive display, not just construction.

    With a discarded identity the sanctioned retry ends
    `--accept-full-replacement <fp> --accept-discarded-identities <fp>`, so the display
    guard cannot assume the approval option is the second-to-last argument.
    """

    storage = (
        '<p>A</p><p>B</p><ac:structured-macro ac:name="info" ac:macro-id="discarded-raw-uuid">'
        "<ac:rich-text-body><p>Old</p></ac:rich-text-body></ac:structured-macro>"
    )
    result, put = _full_replacement_invoke(tmp_path, storage)

    assert result.exit_code == ExitCode.VALIDATION, result.output
    retry = result.output.rstrip().splitlines()[-1]
    assert retry.startswith("Retry: atls confluence page update 12345678 ")
    assert "--accept-full-replacement repl_sha256:" in retry
    assert "--accept-discarded-identities repl_sha256:" in retry
    assert "discarded-raw-uuid" not in result.output
    assert put.called is False


@respx.mock
def test_full_replacement_loss_summary_names_the_whole_page(tmp_path: Path) -> None:
    """What is being approved, in the summary the retry command is gated on.

    A full replacement can carry no migration or conversion occurrence at all, and the
    summary is what stops `_handle_error` from withholding the command. Deriving it from
    the manifest keeps the fail-closed rule (no disclosure, no token) while making the
    disclosure always available on this route.
    """

    result, _put = _full_replacement_invoke(tmp_path, "<p>A</p><p>B</p>")

    assert "Loss summary:" in result.output
    assert "full replacement" in result.output
    assert "discarded identities=0" in result.output
    assert "retry command withheld" not in result.output


# --- what the display guard must refuse ---------------------------------------------
#
# The tests above prove the sanctioned command is shown. These prove nothing else is.
# The displayed string is run verbatim by whoever reads it, so a shape the producer
# never builds must fail closed rather than be normalized into an apparent retry. Round
# one shipped a guard that accepted all four of the first group.

_FULL = "repl_sha256:" + "a" * 64
_MIG = "mig_sha256:" + "b" * 64
_OTHER_FULL = "repl_sha256:" + "e" * 64
_BASE = ["atls", "confluence", "page", "update", "123"]


def _action(code: str, argv: list[str]) -> dict[str, object]:
    return {"id": "retry_with_consent", "requires_user_approval": True, "description_code": code, "argv": argv}


#: Refusal shapes, with what each one is evidence *of*. Five of these were displayed by
#: the first version of this guard and are why it was rewritten; the other five were
#: already refused and are here so a later rewrite cannot quietly lose them. Measured by
#: replaying all ten against the earlier guard rather than assumed, because a refusal
#: test that was already passing proves nothing about the change it ships with.
#:
#:   fixed here   companion_on_migration, companion_on_conversion, duplicate_companion,
#:                second_primary_approval, stray_approval_in_command
#:   regression   companion_fingerprint_mismatch, fingerprint_of_the_wrong_kind,
#:                unknown_description_code, not_an_approval_gated_action,
#:                bare_digest_without_a_prefix (this one only since the prefix check;
#:                the original length-only check accepted it)
_REFUSED = {
    # A companion flag is a property of one consent kind, not of consent in general.
    "companion_on_migration": _action(
        "REVIEW_MIGRATION_AND_RETRY", [*_BASE, "--accept-migration", _MIG, "--accept-discarded-identities", _MIG]
    ),
    "companion_on_conversion": _action(
        "REVIEW_CONVERSION_AND_RETRY",
        [
            *_BASE,
            "--accept-conversion",
            "conv_sha256:" + "c" * 64,
            "--accept-discarded-identities",
            "conv_sha256:" + "c" * 64,
        ],
    ),
    # The producer appends each companion once.
    "duplicate_companion": _action(
        "REVIEW_FULL_REPLACEMENT_AND_RETRY",
        [
            *_BASE,
            "--accept-full-replacement",
            _FULL,
            "--accept-discarded-identities",
            _FULL,
            "--accept-discarded-identities",
            _FULL,
        ],
    ),
    # Two primary approvals is not a shape any candidate binding produces.
    "second_primary_approval": _action(
        "REVIEW_FULL_REPLACEMENT_AND_RETRY",
        [*_BASE, "--accept-full-replacement", _OTHER_FULL, "--accept-full-replacement", _FULL],
    ),
    # An approval flag before the approval section means the command part is not command.
    "stray_approval_in_command": _action(
        "REVIEW_FULL_REPLACEMENT_AND_RETRY",
        [*_BASE, "--accept-discarded-identities", _FULL, "--accept-full-replacement", _FULL],
    ),
    # Each companion repeats the primary fingerprint; two different ones cannot both be
    # the candidate this manifest is bound to.
    "companion_fingerprint_mismatch": _action(
        "REVIEW_FULL_REPLACEMENT_AND_RETRY",
        [*_BASE, "--accept-full-replacement", _FULL, "--accept-discarded-identities", _OTHER_FULL],
    ),
    # The prefix is what keeps an approval of one kind from displaying as another.
    "fingerprint_of_the_wrong_kind": _action(
        "REVIEW_FULL_REPLACEMENT_AND_RETRY", [*_BASE, "--accept-full-replacement", _MIG]
    ),
    "bare_digest_without_a_prefix": _action(
        "REVIEW_FULL_REPLACEMENT_AND_RETRY", [*_BASE, "--accept-full-replacement", "a" * 64]
    ),
    "unknown_description_code": _action("REVIEW_SOMETHING_ELSE", [*_BASE, "--accept-full-replacement", _FULL]),
    "not_an_approval_gated_action": {
        "id": "retry",
        "requires_user_approval": True,
        "description_code": "REVIEW_FULL_REPLACEMENT_AND_RETRY",
        "argv": [*_BASE, "--accept-full-replacement", _FULL],
    },
}


@pytest.mark.parametrize("name", sorted(_REFUSED))
def test_the_display_guard_refuses_every_unsanctioned_retry_shape(name: str) -> None:
    from atlassian_skills.cli.confluence import _consent_retry_display

    assert _consent_retry_display(_REFUSED[name]) is None, f"{name} was displayed as a sanctioned retry"


def test_the_display_guard_still_accepts_both_sanctioned_shapes() -> None:
    """The refusals above are worthless if the guard now refuses everything."""

    from atlassian_skills.cli.confluence import _consent_retry_display

    one_approval = _action("REVIEW_FULL_REPLACEMENT_AND_RETRY", [*_BASE, "--accept-full-replacement", _FULL])
    two_approvals = _action(
        "REVIEW_FULL_REPLACEMENT_AND_RETRY",
        [*_BASE, "--accept-full-replacement", _FULL, "--accept-discarded-identities", _FULL],
    )
    migration = _action("REVIEW_MIGRATION_AND_RETRY", [*_BASE, "--accept-migration", _MIG])
    for action in (one_approval, two_approvals, migration):
        assert _consent_retry_display(action) is not None, action["description_code"]


@pytest.mark.parametrize(
    "context",
    [
        {},
        {"full_replacement": "a raw string where a manifest belongs"},
        {"full_replacement": {}},
        {"full_replacement": {"discarded_identity_count": True}},
        {"full_replacement": {"discarded_identity_count": -1}},
        {"full_replacement": {"discarded_identity_count": "2"}},
    ],
    ids=["missing", "not_a_dict", "count_missing", "count_is_a_bool", "count_negative", "count_is_a_string"],
)
def test_a_malformed_replacement_manifest_discloses_nothing(context: dict[str, object]) -> None:
    """No valid disclosure, no approval token.

    `_handle_error` gates the retry command on a non-empty loss summary, so anything that
    yields a summary here unlocks the command. A negative count did exactly that in round
    one: it is not a disclosure, it is a malformed one.
    """

    from atlassian_skills.cli.confluence import _consent_loss_summary

    assert _consent_loss_summary(context) is None
