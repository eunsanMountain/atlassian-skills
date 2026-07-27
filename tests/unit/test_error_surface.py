"""The human-readable error must name the request it failed on (GitHub #19).

Failure mode this guards: `atls jira issue search ...` behind a corporate proxy
printed exactly `Error: HTTP 302` while `--format=json` carried `http_url`. The
reporter had to switch formats to discover atls was calling
`/rest/api/2/search`, not the base URL they had configured.

The line has to land in all four renderers. `cli/main.py` only sees errors that
escape a command, and each product module catches `AtlasError` in its own
`_handle_error` — patching the entrypoint alone would leave the ~119 real
command call sites unchanged.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from atlassian_skills.cli.bitbucket import _handle_error as bitbucket_handle_error
from atlassian_skills.cli.confluence import _handle_error as confluence_handle_error
from atlassian_skills.cli.jira import _handle_error as jira_handle_error
from atlassian_skills.cli.main import _emit_entrypoint_error
from atlassian_skills.core.errors import (
    AtlasError,
    NotFoundError,
    RedirectError,
    request_context_line,
    safe_display_url,
)
from atlassian_skills.core.format import OutputFormat

Renderer = Callable[[AtlasError], None]

_PRODUCT_RENDERERS: dict[str, Renderer] = {
    "jira": lambda err: jira_handle_error(err, OutputFormat.COMPACT),
    "confluence": lambda err: confluence_handle_error(err, OutputFormat.COMPACT),
    "bitbucket": lambda err: bitbucket_handle_error(err, OutputFormat.COMPACT),
}


def _redirect_error() -> RedirectError:
    return RedirectError(
        "Server returned 302 with no usable Location header",
        http_status=302,
        http_url="https://jira.example.com/rest/api/2/search",
        http_method="GET",
        context={"reason": "redirect_without_location"},
    )


@pytest.mark.parametrize("product", sorted(_PRODUCT_RENDERERS))
def test_product_renderers_emit_the_request_line(product: str, capsys: pytest.CaptureFixture[str]) -> None:
    import typer

    with pytest.raises(typer.Exit):
        _PRODUCT_RENDERERS[product](_redirect_error())
    err = capsys.readouterr().err
    assert "Request: GET https://jira.example.com/rest/api/2/search -> 302" in err


def test_entrypoint_renderer_emits_the_request_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["atls", "jira", "issue", "search", "project=X"])
    _emit_entrypoint_error(_redirect_error())
    assert "Request: GET https://jira.example.com/rest/api/2/search -> 302" in capsys.readouterr().err


def test_json_output_has_no_request_line(capsys: pytest.CaptureFixture[str]) -> None:
    """The JSON envelope already carries http_url; it must stay byte-identical."""
    import json

    import typer

    with pytest.raises(typer.Exit):
        jira_handle_error(_redirect_error(), OutputFormat.JSON)
    out = capsys.readouterr().out
    assert "Request:" not in out
    payload = json.loads(out)
    assert payload["error"]["http_url"] == "https://jira.example.com/rest/api/2/search"


def test_confluence_diagnostic_output_is_preserved(capsys: pytest.CaptureFixture[str]) -> None:
    """The Confluence renderer carries consent/patch diagnostics the others do not.

    Injecting one line must not disturb them — that is why the shared helper is a
    single line rather than a unified renderer.
    """
    import typer

    err = AtlasError(
        "Text matched in more than one place",
        http_status=400,
        http_url="https://confluence.example.com/rest/api/content/1",
        http_method="PUT",
        context={"match_count": 3, "excluded_match_count": 1},
    )
    with pytest.raises(typer.Exit):
        confluence_handle_error(err, OutputFormat.COMPACT)
    err_text = capsys.readouterr().err
    assert "Request: PUT https://confluence.example.com/rest/api/content/1 -> 400" in err_text
    assert "Found: 3 matches, 1 in attributes or macros" in err_text


def test_no_request_line_without_http_metadata() -> None:
    assert request_context_line(NotFoundError("plain local failure")) is None


class TestSafeDisplayUrl:
    """A redirect Location becomes `http_url` (client.py replaces `url` when it
    follows one), so the display path must scrub credentials, not just format."""

    def test_query_and_fragment_are_dropped(self) -> None:
        url = "https://jira.example.com/login.action?os_destination=%2Fsecret&token=abc#frag"
        cleaned = safe_display_url(url)
        assert cleaned == "https://jira.example.com/login.action"
        assert "token" not in cleaned and "os_destination" not in cleaned

    def test_userinfo_is_dropped(self) -> None:
        assert safe_display_url("https://user:hunter2@jira.example.com/rest") == "https://jira.example.com/rest"

    def test_control_characters_are_stripped(self) -> None:
        cleaned = safe_display_url("https://jira.example.com/a\r\nInjected: header")
        assert "\n" not in cleaned and "\r" not in cleaned

    def test_ipv6_host_keeps_brackets(self) -> None:
        assert safe_display_url("https://[::1]:8443/rest/api/2/myself") == "https://[::1]:8443/rest/api/2/myself"

    def test_port_is_preserved(self) -> None:
        assert safe_display_url("http://127.0.0.1:8765/rest") == "http://127.0.0.1:8765/rest"

    def test_malformed_authority_does_not_leak_query(self) -> None:
        cleaned = safe_display_url("https://host:notaport/path?token=abc")
        assert "token=abc" not in cleaned

    def test_empty_input(self) -> None:
        assert safe_display_url(None) == ""
        assert safe_display_url("") == ""

    def test_long_url_is_truncated(self) -> None:
        assert len(safe_display_url("https://jira.example.com/" + "a" * 5000)) < 400
