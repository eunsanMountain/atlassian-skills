"""Rebuilt responses must not keep `content-encoding` (GitHub #16 follow-up).

`request()` streams the body and rebuilds an `httpx.Response` from the bytes
`iter_bytes()` yields — which are already content-decoded. Copying the original
headers wholesale left `content-encoding: gzip` on the rebuilt response, so
httpx ran the gzip decoder a second time over already-plain bytes and every
compressed API response died with
`Connection error: Error -3 while decompressing data: incorrect header check`.

A 0.3.0 regression: the streaming rewrite introduced the rebuild, 0.2.x used the
response httpx returned. The mock suite never noticed because every fixture is
served uncompressed — these tests are the compressed coverage.
"""

from __future__ import annotations

import gzip
import zlib

import httpx
import pytest
import respx

from atlassian_skills.core.auth import Credential
from atlassian_skills.core.client import BaseClient
from atlassian_skills.core.errors import NotFoundError, RedirectError

BASE_URL = "https://jira.example.com"


def make_client() -> BaseClient:
    return BaseClient(BASE_URL, Credential(method="pat", token="t"))


def gzip_response(status: int, body: bytes, **extra_headers: str) -> httpx.Response:
    compressed = gzip.compress(body)
    return httpx.Response(
        status,
        content=compressed,
        headers={
            "content-encoding": "gzip",
            "content-type": "application/json",
            "content-length": str(len(compressed)),
            **extra_headers,
        },
    )


@respx.mock
def test_gzip_success_response_parses() -> None:
    respx.get(f"{BASE_URL}/rest/api/2/myself").mock(return_value=gzip_response(200, b'{"name": "u"}'))
    response = make_client().get("/rest/api/2/myself")
    assert response.json() == {"name": "u"}
    # The stale encoding header is the regression. The wire-side content-length
    # is dropped too, but httpx re-derives an accurate one from the decoded body.
    assert "content-encoding" not in response.headers
    assert response.headers.get("content-length") == str(len(b'{"name": "u"}'))


@respx.mock
def test_deflate_success_response_parses() -> None:
    body = zlib.compress(b'{"ok": true}')
    respx.get(f"{BASE_URL}/rest/api/2/myself").mock(
        return_value=httpx.Response(
            200,
            content=body,
            headers={"content-encoding": "deflate", "content-type": "application/json"},
        )
    )
    assert make_client().get("/rest/api/2/myself").json() == {"ok": True}


@respx.mock
def test_gzip_error_body_is_still_diagnosed() -> None:
    respx.get(f"{BASE_URL}/rest/api/2/issue/NOPE-1").mock(
        return_value=gzip_response(404, b'{"errorMessages": ["Issue does not exist"]}')
    )
    with pytest.raises(NotFoundError) as exc:
        make_client().get("/rest/api/2/issue/NOPE-1")
    assert "Issue does not exist" in exc.value.message


@respx.mock
def test_gzip_redirect_error_keeps_redirect_diagnosis() -> None:
    """The #19 fixture with compression on top: the 3xx diagnosis must survive."""
    respx.get(f"{BASE_URL}/rest/api/2/myself").mock(return_value=gzip_response(302, b""))
    with pytest.raises(RedirectError) as exc:
        make_client().get("/rest/api/2/myself")
    assert (exc.value.context or {}).get("reason") == "redirect_without_location"


@respx.mock
def test_retry_path_tolerates_gzip_headers() -> None:
    respx.get(f"{BASE_URL}/rest/api/2/myself").mock(
        side_effect=[
            gzip_response(503, b"", **{"retry-after": "0"}),
            gzip_response(200, b'{"ok": 1}'),
        ]
    )
    assert make_client().get("/rest/api/2/myself").json() == {"ok": 1}
