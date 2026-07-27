"""Guarded redirect following (Confluence attachment downloads use 302).

Failure mode: a pull-md failed on a served HTTP 302 for
`/download/attachments/<id>/<file>?version=...` because the client treated
every 3xx as a hard error. Redirects are followed
only for safe methods, only on the configured origin, never into a login flow,
and never more than _MAX_REDIRECTS hops.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from atlassian_skills.core.auth import Credential
from atlassian_skills.core.client import BaseClient
from atlassian_skills.core.errors import AtlasError, AuthError, ValidationError

BASE_URL = "https://confluence.example.com"
PAT_CRED = Credential(method="pat", token="test-token")


def make_client(**kwargs: object) -> BaseClient:
    return BaseClient(BASE_URL, PAT_CRED, **kwargs)


@respx.mock
def test_get_follows_same_origin_redirect() -> None:
    # Register the more specific route first: respx matches in order.
    respx.get(f"{BASE_URL}/download/attachments/1/a.png", params={"version": "2"}).mock(
        return_value=httpx.Response(200, content=b"PNGBYTES")
    )
    respx.get(f"{BASE_URL}/download/attachments/1/a.png").mock(
        return_value=httpx.Response(302, headers={"location": f"{BASE_URL}/download/attachments/1/a.png?version=2"})
    )
    resp = make_client().get("/download/attachments/1/a.png")
    assert resp.status_code == 200
    assert resp.content == b"PNGBYTES"


@respx.mock
def test_get_follows_relative_redirect() -> None:
    respx.get(f"{BASE_URL}/download/attachments/1/a.png").mock(
        return_value=httpx.Response(302, headers={"location": "/download/final/a.png"})
    )
    respx.get(f"{BASE_URL}/download/final/a.png").mock(return_value=httpx.Response(200, content=b"OK"))
    resp = make_client().get("/download/attachments/1/a.png")
    assert resp.content == b"OK"


@respx.mock
def test_redirect_does_not_reapply_original_params() -> None:
    # The Location URL carries its own (possibly different) query; re-applying
    # the original request params would corrupt it.
    route = respx.get(f"{BASE_URL}/download/final/a.png")
    route.mock(return_value=httpx.Response(200, content=b"OK"))
    respx.get(f"{BASE_URL}/download/attachments/1/a.png", params={"api": "v2"}).mock(
        return_value=httpx.Response(302, headers={"location": f"{BASE_URL}/download/final/a.png"})
    )
    make_client().get("/download/attachments/1/a.png", params={"api": "v2"})
    assert "api" not in str(route.calls.last.request.url)


@respx.mock
def test_cross_origin_redirect_is_rejected() -> None:
    respx.get(f"{BASE_URL}/download/attachments/1/a.png").mock(
        return_value=httpx.Response(302, headers={"location": "https://evil.example.net/a.png"})
    )
    with pytest.raises(ValidationError) as exc:
        make_client().get("/download/attachments/1/a.png")
    assert exc.value.context.get("reason") == "unsafe_redirect"


@respx.mock
def test_login_redirect_becomes_auth_error() -> None:
    # An expired PAT/session redirects to /login.action; following it would
    # store login-page HTML as attachment bytes.
    respx.get(f"{BASE_URL}/download/attachments/1/a.png").mock(
        return_value=httpx.Response(302, headers={"location": f"{BASE_URL}/login.action?os_destination=%2Fx"})
    )
    with pytest.raises(AuthError) as exc:
        make_client().get("/download/attachments/1/a.png")
    assert exc.value.context.get("reason") == "redirected_to_login"


@respx.mock
def test_attachment_named_login_still_downloads() -> None:
    """The login guard matches the login *endpoint*, not the substring.

    Regression: `"login" in path` also matched the attachment filename, so a
    page carrying `login.png` (login-screen screenshots are common in docs)
    aborted its pull with a false `redirected_to_login` auth error — defeating
    the very 302-following this guard protects.
    """
    for name in ("login.png", "weblogin-diagram.png", "sso-login-flow.pdf"):
        respx.get(f"{BASE_URL}/download/attachments/1/{name}", params={"version": "2"}).mock(
            return_value=httpx.Response(200, content=b"BYTES")
        )
        respx.get(f"{BASE_URL}/download/attachments/1/{name}").mock(
            return_value=httpx.Response(
                302, headers={"location": f"{BASE_URL}/download/attachments/1/{name}?version=2"}
            )
        )
        resp = make_client().get(f"/download/attachments/1/{name}")
        assert resp.content == b"BYTES", name


@respx.mock
def test_jira_login_jsp_redirect_still_becomes_auth_error() -> None:
    respx.get(f"{BASE_URL}/rest/api/2/issue/X").mock(
        return_value=httpx.Response(302, headers={"location": f"{BASE_URL}/login.jsp?os_destination=%2Fx"})
    )
    with pytest.raises(AuthError) as exc:
        make_client().get("/rest/api/2/issue/X")
    assert exc.value.context.get("reason") == "redirected_to_login"


@respx.mock
def test_redirect_loop_is_bounded() -> None:
    respx.get(f"{BASE_URL}/loop").mock(return_value=httpx.Response(302, headers={"location": f"{BASE_URL}/loop"}))
    with pytest.raises(AtlasError) as exc:
        make_client().get("/loop")
    assert getattr(exc.value, "http_status", None) == 302


@respx.mock
def test_mutating_method_redirect_is_not_followed() -> None:
    respx.put(f"{BASE_URL}/rest/api/content/1").mock(
        return_value=httpx.Response(302, headers={"location": f"{BASE_URL}/elsewhere"})
    )
    with pytest.raises(AtlasError) as exc:
        make_client().put("/rest/api/content/1", json={})
    assert getattr(exc.value, "http_status", None) == 302


@respx.mock
def test_redirect_without_location_is_an_error() -> None:
    respx.get(f"{BASE_URL}/download/attachments/1/a.png").mock(return_value=httpx.Response(302))
    with pytest.raises(AtlasError) as exc:
        make_client().get("/download/attachments/1/a.png")
    assert getattr(exc.value, "http_status", None) == 302


# ---------------------------------------------------------------------------
# 3xx diagnosis (GitHub #19)
#
# Every path below used to fall through to the generic mapper and surface as a
# bare "HTTP 302" with the Location header thrown away. Exit codes are asserted
# explicitly because agents branch on them: the two new generic cases must stay
# at 1, and the pre-existing login / cross-origin cases must stay at 6 and 7.
# ---------------------------------------------------------------------------

from atlassian_skills.core.errors import ExitCode, RedirectError  # noqa: E402


@respx.mock
def test_redirect_without_location_is_diagnosed() -> None:
    respx.get(f"{BASE_URL}/rest/api/2/myself").mock(return_value=httpx.Response(302))
    with pytest.raises(RedirectError) as exc:
        make_client().get("/rest/api/2/myself")
    assert (exc.value.context or {}).get("reason") == "redirect_without_location"
    assert exc.value.exit_code == ExitCode.GENERIC


@respx.mock
def test_too_many_redirects_is_diagnosed() -> None:
    # Same-origin loop: allowed by the origin guard, stopped by the hop budget.
    respx.get(f"{BASE_URL}/loop").mock(return_value=httpx.Response(302, headers={"location": f"{BASE_URL}/loop"}))
    with pytest.raises(RedirectError) as exc:
        make_client().get("/loop")
    context = exc.value.context or {}
    assert context.get("reason") == "too_many_redirects"
    assert context.get("redirects") == 6
    assert exc.value.exit_code == ExitCode.GENERIC


@respx.mock
def test_post_redirect_is_reported_but_not_followed() -> None:
    """A proxy bouncing a write used to be completely opaque.

    The Location is collected for diagnosis, but following it is still refused —
    replaying a request body against a new target is not safe.
    """
    target = respx.post(f"{BASE_URL}/rest/api/2/issue").mock(
        return_value=httpx.Response(302, headers={"location": f"{BASE_URL}/proxy-auth"})
    )
    followed = respx.get(f"{BASE_URL}/proxy-auth").mock(return_value=httpx.Response(200))
    with pytest.raises(RedirectError) as exc:
        make_client().post("/rest/api/2/issue", json={"fields": {}})
    context = exc.value.context or {}
    assert context.get("reason") == "redirect_not_followed"
    assert context.get("location") == f"{BASE_URL}/proxy-auth"
    assert exc.value.exit_code == ExitCode.GENERIC
    assert target.call_count == 1
    assert followed.call_count == 0


@respx.mock
def test_redirect_error_message_names_the_target() -> None:
    respx.get(f"{BASE_URL}/rest/api/2/myself").mock(
        return_value=httpx.Response(302, headers={"location": f"{BASE_URL}/rest/api/2/myself"})
    )
    with pytest.raises(RedirectError) as exc:
        make_client().get("/rest/api/2/myself")
    assert "redirect to" in exc.value.message
    assert exc.value.hint is not None and "NO_PROXY" in exc.value.hint


@respx.mock
def test_redirect_location_in_context_is_scrubbed() -> None:
    respx.get(f"{BASE_URL}/loop").mock(
        return_value=httpx.Response(302, headers={"location": f"{BASE_URL}/loop?session=secret-token"})
    )
    with pytest.raises(RedirectError) as exc:
        make_client().get("/loop")
    assert "secret-token" not in str(exc.value.context)
    assert "secret-token" not in exc.value.message


@respx.mock
def test_login_redirect_keeps_auth_exit_code_and_gains_metadata() -> None:
    respx.get(f"{BASE_URL}/rest/api/2/myself").mock(
        return_value=httpx.Response(302, headers={"location": f"{BASE_URL}/login.action"})
    )
    with pytest.raises(AuthError) as exc:
        make_client().get("/rest/api/2/myself")
    assert exc.value.exit_code == ExitCode.AUTH
    # Needed by the `Request:` line — the error carried no http_* fields before.
    assert exc.value.http_status == 302
    assert exc.value.http_method == "GET"
    assert exc.value.http_url is not None


@respx.mock
def test_cross_origin_keeps_validation_exit_code_and_names_the_host() -> None:
    respx.get(f"{BASE_URL}/rest/api/2/myself").mock(
        return_value=httpx.Response(302, headers={"location": "https://proxy.corp.example.net/auth"})
    )
    with pytest.raises(ValidationError) as exc:
        make_client().get("/rest/api/2/myself")
    context = exc.value.context or {}
    assert context.get("reason") == "unsafe_redirect"
    assert context.get("target_host") == "proxy.corp.example.net"
    assert exc.value.exit_code == ExitCode.VALIDATION
    assert exc.value.http_url is not None
