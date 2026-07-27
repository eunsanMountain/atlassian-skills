"""`--verbose` must actually do something, and must never leak a secret.

Before 0.3.1 the flag was declared on the root callback and stored in `ctx.obj`,
but no code read it: `atls --verbose 3 ...` was accepted and silently ignored,
which reads worse than having no flag at all (GitHub #17).

The security half matters more than the feature half. This stderr lands in agent
transcripts and in logs users paste into bug reports, and `request()` merges the
PAT into every request's headers.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from atlassian_skills.core.auth import Credential
from atlassian_skills.core.client import BaseClient
from atlassian_skills.core.errors import AtlasError

BASE_URL = "https://jira.example.com"
SECRET = "pat-super-secret-value"
PAT_CRED = Credential(method="pat", token=SECRET)


def make_client(verbose: int) -> BaseClient:
    return BaseClient(BASE_URL, PAT_CRED, verbose=verbose)


@respx.mock
def test_verbose_zero_is_silent(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE_URL}/rest/api/2/myself").mock(return_value=httpx.Response(200, json={"name": "u"}))
    make_client(0).get("/rest/api/2/myself")
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


@respx.mock
def test_verbose_one_logs_one_line_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE_URL}/rest/api/2/myself").mock(return_value=httpx.Response(200, json={"name": "u"}))
    make_client(1).get("/rest/api/2/myself")
    captured = capsys.readouterr()
    assert "[atls] GET https://jira.example.com/rest/api/2/myself -> 200" in captured.err
    # stdout is the machine-readable surface; verbose must never touch it.
    assert captured.out == ""


@respx.mock
def test_authorization_header_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE_URL}/rest/api/2/myself").mock(return_value=httpx.Response(200, json={"name": "u"}))
    make_client(3).get("/rest/api/2/myself")
    captured = capsys.readouterr()
    assert SECRET not in captured.err
    assert SECRET not in captured.out
    assert "<redacted len=" in captured.err


@respx.mock
def test_masking_is_case_insensitive(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE_URL}/rest/api/2/myself").mock(
        return_value=httpx.Response(
            200,
            json={"name": "u"},
            headers={"Set-Cookie": "JSESSIONID=abcdef; Path=/", "X-Atlassian-Token": "nocheck"},
        )
    )
    make_client(2).get("/rest/api/2/myself")
    err = capsys.readouterr().err
    assert "JSESSIONID=abcdef" not in err
    assert "nocheck" not in err


@respx.mock
def test_location_is_sanitized_not_hidden(capsys: pytest.CaptureFixture[str]) -> None:
    """Location is the one server header worth showing — a proxy bounce is
    invisible without it — so it is scrubbed rather than masked."""
    respx.get(f"{BASE_URL}/rest/api/2/search").mock(
        return_value=httpx.Response(
            302,
            headers={"location": "https://jira.example.com/login.action?os_destination=%2Fx&token=leaky"},
        )
    )
    with pytest.raises(AtlasError):
        make_client(2).get("/rest/api/2/search")
    err = capsys.readouterr().err
    assert "location: https://jira.example.com/login.action" in err
    assert "token=leaky" not in err
    assert "os_destination" not in err


@respx.mock
def test_request_url_query_is_not_logged(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE_URL}/rest/api/2/search").mock(return_value=httpx.Response(200, json={"issues": []}))
    make_client(1).get("/rest/api/2/search", params={"jql": "project=SECRETPROJ"})
    err = capsys.readouterr().err
    assert "SECRETPROJ" not in err


@respx.mock
def test_response_body_is_never_printed(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE_URL}/rest/api/2/myself").mock(
        return_value=httpx.Response(200, json={"name": "u", "emailAddress": "someone@corp.example.com"})
    )
    make_client(3).get("/rest/api/2/myself")
    err = capsys.readouterr().err
    # Key *names* are shape metadata and are allowed; the values are not.
    assert "emailAddress" in err
    assert "someone@corp.example.com" not in err


@respx.mock
def test_json_key_list_is_capped(capsys: pytest.CaptureFixture[str]) -> None:
    payload = {f"field{i}": i for i in range(80)}
    respx.get(f"{BASE_URL}/rest/api/2/myself").mock(return_value=httpx.Response(200, json=payload))
    make_client(3).get("/rest/api/2/myself")
    err = capsys.readouterr().err
    assert "(+50 more)" in err


@respx.mock
def test_long_key_is_truncated(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE_URL}/rest/api/2/myself").mock(return_value=httpx.Response(200, json={"k" * 500: 1}))
    make_client(3).get("/rest/api/2/myself")
    err = capsys.readouterr().err
    assert "k" * 500 not in err
    assert "..." in err


@respx.mock
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-content-length"),
        pytest.param({"content-length": "5"}, id="content-length-lies-small"),
        pytest.param({"content-length": "not-a-number"}, id="content-length-malformed"),
    ],
)
def test_large_body_skips_key_parsing_regardless_of_content_length(
    headers: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """The cap keys off the real body size.

    Trusting `Content-Length` would let a chunked response (no header) or a lying
    one drag a multi-megabyte body through `json()` just to print key names.
    """
    payload = json.dumps({"bigfield": "x" * (300 * 1024)}).encode()
    respx.get(f"{BASE_URL}/rest/api/2/myself").mock(
        return_value=httpx.Response(200, content=payload, headers={"content-type": "application/json", **headers})
    )
    make_client(3).get("/rest/api/2/myself")
    err = capsys.readouterr().err
    assert "keys omitted" in err
    assert "bigfield" not in err


@respx.mock
def test_proxy_env_is_reported_with_credentials_stripped(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxyuser:proxypass@proxy.corp.example.com:8080")
    monkeypatch.setenv("NO_PROXY", ".corp.example.com")
    respx.get(f"{BASE_URL}/rest/api/2/myself").mock(return_value=httpx.Response(200, json={"name": "u"}))
    make_client(2).get("/rest/api/2/myself")
    err = capsys.readouterr().err
    assert "proxy.corp.example.com:8080" in err
    assert "proxypass" not in err
    assert "NO_PROXY=.corp.example.com" in err


@respx.mock
def test_transport_error_is_logged(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE_URL}/rest/api/2/myself").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(AtlasError):
        make_client(1).get("/rest/api/2/myself")
    assert "transport error ConnectError" in capsys.readouterr().err
