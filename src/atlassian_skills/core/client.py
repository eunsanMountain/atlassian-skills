from __future__ import annotations

import contextlib
import os
import re
import ssl
import sys
import time
from typing import Any

import httpx

from atlassian_skills.core.auth import Credential
from atlassian_skills.core.errors import (
    CONNECTION_HINT,
    PROXY_HINT,
    AuthError,
    NetworkError,
    RedirectInfo,
    ValidationError,
    http_error_to_atlas,
    safe_display_url,
    safe_header_value,
)
from atlassian_skills.core.pagination import collect_all, paginate_links, paginate_offset

_RETRY_STATUSES = {429, 500, 502, 503, 504}

# Confluence Server/DC serves attachment downloads (and some long-lived URLs)
# through 3xx redirects. Follow them only for safe methods, only on the
# configured origin, and never into a login flow (an expired session redirects
# to /login.action, whose HTML must not be mistaken for attachment bytes).
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5
# Match the login *endpoint*, not the substring. Attachment download paths carry
# the filename (/download/attachments/<id>/login.png, weblogin-diagram.png), so a
# bare `"login" in path` check aborts legitimate downloads with a false auth error
# and defeats the redirect-following this guard exists to protect.
_LOGIN_PATH_RE = re.compile(r"/(?:do)?login(?:\.action|\.jsp)?(?:$|[/?])", re.IGNORECASE)
MAX_ATTACHMENT_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_API_RESPONSE_BYTES = 100 * 1024 * 1024
MAX_ERROR_RESPONSE_BYTES = 64 * 1024

# --verbose rendering limits. The stderr this produces lands in agent
# transcripts and in bug reports users paste, so nothing here may echo a
# credential or a response body.
_MASKED_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-atlassian-token",
    }
)
MAX_VERBOSE_HEADER_LEN = 256
MAX_VERBOSE_KEY_LEN = 64
MAX_VERBOSE_JSON_KEYS = 30
# Guard on the *actual* body size, never on Content-Length: a chunked response
# has no such header and a lying one would let a 100MB body through.
MAX_VERBOSE_JSON_BYTES = 256 * 1024


def _effective_port(url: httpx.URL) -> int | None:
    if url.port is not None:
        return url.port
    return {"http": 80, "https": 443}.get(url.scheme)


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return url.scheme, url.host, _effective_port(url)


def _decoded_headers(headers: httpx.Headers) -> httpx.Headers:
    """Headers for a rebuilt response whose body has already been content-decoded.

    `iter_bytes()` yields *decoded* bytes, so the rebuilt response must not keep
    the original `content-encoding` — httpx would run the gzip decoder a second
    time over already-plain bytes and every compressed API response would die
    with `zlib.error: incorrect header check` (GitHub #16 follow-up, a 0.3.0
    regression). `content-length` describes the wire size, not the decoded size,
    so it is dropped for the same reason.
    """
    cleaned = httpx.Headers(headers)
    cleaned.pop("content-encoding", None)
    cleaned.pop("content-length", None)
    return cleaned


def _read_bounded_response(response: httpx.Response, max_bytes: int) -> httpx.Response:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = None
        if declared_size is not None and declared_size > max_bytes:
            raise ValidationError(
                "Response exceeds the configured download size limit",
                context={
                    "reason": "response_too_large",
                    "max_bytes": max_bytes,
                    "content_length": declared_size,
                },
            )
    content = bytearray()
    for chunk in response.iter_bytes():
        if len(content) + len(chunk) > max_bytes:
            raise ValidationError(
                "Response exceeds the configured download size limit",
                context={"reason": "response_too_large", "max_bytes": max_bytes},
            )
        content.extend(chunk)
    return httpx.Response(
        response.status_code,
        headers=_decoded_headers(response.headers),
        content=bytes(content),
        request=response.request,
        extensions=response.extensions,
    )


def _read_error_response(response: httpx.Response) -> httpx.Response:
    content = bytearray()
    for chunk in response.iter_bytes():
        remaining = MAX_ERROR_RESPONSE_BYTES - len(content)
        if remaining <= 0:
            break
        content.extend(chunk[:remaining])
        if len(chunk) > remaining:
            break
    return httpx.Response(
        response.status_code,
        headers=_decoded_headers(response.headers),
        content=bytes(content),
        request=response.request,
        extensions=response.extensions,
    )


class BaseClient:
    def __init__(
        self,
        base_url: str,
        credential: Credential,
        timeout: float = 30.0,
        max_retries: int = 3,
        verify: ssl.SSLContext | bool = True,
        verbose: int = 0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.credential = credential
        self.timeout = timeout
        self.max_retries = max_retries
        self.verbose = verbose
        try:
            self._client = httpx.Client(timeout=timeout, verify=verify)
        except OSError as error:  # ssl.SSLError is an OSError: a broken SSL_CERT_FILE/DIR lands here
            env_sources = [name for name in ("SSL_CERT_FILE", "SSL_CERT_DIR") if os.environ.get(name)]
            context: dict[str, Any] = {"reason": "invalid_trust_store"}
            if env_sources:
                context["env"] = ",".join(env_sources)
            raise ValidationError(
                f"TLS trust store could not be loaded ({type(error).__name__}: {safe_header_value(str(error), 200)})",
                hint=(
                    "The certificate source could not be read as PEM. Run `atls doctor` to see which "
                    "trust source is in effect and whether it loads. Explicit configuration wins over "
                    "the OS trust store, so unset SSL_CERT_FILE/SSL_CERT_DIR to fall back to the system "
                    "certificates. A broken SSL_CERT_FILE affects every tool that reads it (uv, pip), "
                    "not just atls."
                ),
                context=context,
            ) from error

    # ------------------------------------------------------------------
    # Core request with retry
    # ------------------------------------------------------------------

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        files: Any = None,
        headers: dict[str, str] | None = None,
        max_response_bytes: int | None = None,
        retryable: bool = True,
    ) -> httpx.Response:
        if max_response_bytes is not None and max_response_bytes < 1:
            raise ValueError("max_response_bytes must be at least 1")
        try:
            candidate = httpx.URL(path)
        except (httpx.InvalidURL, ValueError) as error:
            raise ValidationError(
                "Request URL is malformed",
                context={"reason": "invalid_request_url"},
            ) from error
        if candidate.is_absolute_url:
            base = httpx.URL(self.base_url)
            if candidate.username or candidate.password or _origin(candidate) != _origin(base):
                raise ValidationError(
                    "Absolute request URL must match the configured Atlassian origin",
                    context={"reason": "unsafe_absolute_url"},
                )
            url = path
        else:
            url = f"{self.base_url}/{path.lstrip('/')}"
        merged_headers = {**self.credential.to_header(), **(headers or {})}

        attempt = 0
        delay = 1.0
        redirects = 0
        total_start = time.monotonic()
        max_total_retry_seconds = 90.0

        while True:
            started = time.monotonic()
            if self.verbose >= 2:
                _log_headers(">", merged_headers)
                _log_proxy_env()
            try:
                response_limit = max_response_bytes or MAX_API_RESPONSE_BYTES
                with self._client.stream(
                    method,
                    url,
                    params=params,
                    json=json,
                    data=data,
                    files=files,
                    headers=merged_headers,
                ) as streamed:
                    if retryable and streamed.status_code in _RETRY_STATUSES and attempt < self.max_retries:
                        response = httpx.Response(
                            streamed.status_code,
                            headers=_decoded_headers(streamed.headers),
                            content=b"",
                            request=streamed.request,
                            extensions=streamed.extensions,
                        )
                    elif streamed.is_success:
                        response = _read_bounded_response(streamed, response_limit)
                    else:
                        response = _read_error_response(streamed)
            except httpx.TimeoutException as exc:
                if self.verbose:
                    _vlog(f"{method} {safe_display_url(url)} -> timeout ({self._elapsed_ms(started)}ms)")
                raise NetworkError(
                    f"Request timed out: {method} {url}",
                    hint=CONNECTION_HINT,
                    http_url=url,
                    http_method=method,
                ) from exc
            except httpx.RequestError as exc:
                if self.verbose:
                    _vlog(
                        f"{method} {safe_display_url(url)} -> transport error "
                        f"{type(exc).__name__} ({self._elapsed_ms(started)}ms)"
                    )
                raise NetworkError(
                    f"Connection error: {exc}",
                    hint=CONNECTION_HINT,
                    http_url=url,
                    http_method=method,
                ) from exc

            if self.verbose:
                _vlog(f"{method} {safe_display_url(url)} -> {response.status_code} ({self._elapsed_ms(started)}ms)")
                if self.verbose >= 2:
                    _log_headers("<", response.headers)
                if self.verbose >= 3:
                    _vlog(f"  body {_body_summary(response)}")

            if retryable and response.status_code in _RETRY_STATUSES and attempt < self.max_retries:
                elapsed = time.monotonic() - total_start
                if elapsed >= max_total_retry_seconds:
                    # Retry budget exhausted — fall through to error handling below
                    _warn_retry(attempt, self.max_retries, 0, response.status_code)
                    break
                attempt += 1
                wait = _retry_wait(response, delay)
                # Cap wait to remaining budget
                wait = min(wait, max_total_retry_seconds - elapsed)
                _warn_retry(attempt, self.max_retries, wait, response.status_code)
                time.sleep(wait)
                delay *= 2
                continue

            # Success range
            if response.is_success:
                return response

            # Guarded redirect handling. Following stays restricted to safe
            # methods, but the Location is *collected* for every 3xx: a POST that
            # a corporate proxy bounces used to surface as a bare "HTTP 302" with
            # no clue where it was sent (GitHub #19).
            redirect_info: RedirectInfo | None = None
            if response.status_code in _REDIRECT_STATUSES:
                location = response.headers.get("location")
                if method.upper() in ("GET", "HEAD"):
                    redirects += 1
                    if location and redirects <= _MAX_REDIRECTS:
                        try:
                            target = httpx.URL(url).join(location)
                        except (httpx.InvalidURL, ValueError) as error:
                            raise ValidationError(
                                "Redirect target URL is malformed",
                                http_status=response.status_code,
                                http_url=url,
                                http_method=method,
                                context={"reason": "invalid_redirect_url"},
                            ) from error
                        base = httpx.URL(self.base_url)
                        if target.username or target.password or _origin(target) != _origin(base):
                            raise ValidationError(
                                "Redirect target must stay on the configured Atlassian origin",
                                hint=PROXY_HINT,
                                http_status=response.status_code,
                                http_url=url,
                                http_method=method,
                                context={
                                    "reason": "unsafe_redirect",
                                    "http_status": response.status_code,
                                    "target_host": safe_header_value(target.host),
                                },
                            )
                        if _LOGIN_PATH_RE.search(target.path):
                            raise AuthError(
                                "Server redirected to a login page; the session or token is not accepted",
                                http_status=response.status_code,
                                http_url=url,
                                http_method=method,
                                context={"reason": "redirected_to_login"},
                            )
                        # The Location URL carries its own query; do not re-apply params.
                        url = str(target)
                        params = None
                        continue
                    redirect_info = RedirectInfo(
                        reason="too_many_redirects" if location else "redirect_without_location",
                        location=location,
                        redirects=redirects,
                    )
                else:
                    # Never follow a redirect for a mutating method — replaying the
                    # body against a new target is not safe. Report it instead.
                    redirect_info = RedirectInfo(reason="redirect_not_followed", location=location)

            # Non-retryable error
            body: str | None = None
            with contextlib.suppress(Exception):
                body = response.text

            raise http_error_to_atlas(response.status_code, url, method, body, redirect=redirect_info)

        # Retry budget exhausted — report the actual HTTP error
        body_text: str | None = None
        with contextlib.suppress(Exception):
            body_text = response.text
        raise http_error_to_atlas(response.status_code, url, method, body_text)

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        max_response_bytes: int | None = None,
    ) -> httpx.Response:
        return self.request("GET", path, params=params, max_response_bytes=max_response_bytes)

    def post(
        self,
        path: str,
        *,
        json: Any = None,
        data: Any = None,
    ) -> httpx.Response:
        return self.request("POST", path, json=json, data=data)

    def put(self, path: str, *, json: Any = None) -> httpx.Response:
        return self.request("PUT", path, json=json)

    def delete(self, path: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        return self.request("DELETE", path, params=params)

    # ------------------------------------------------------------------
    # Pagination helpers
    # ------------------------------------------------------------------

    def get_paginated_offset(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        items_key: str = "issues",
        limit: int | None = None,
        max_results_per_page: int = 50,
    ) -> list[Any]:
        base_params = dict(params or {})

        def fetch(start_at: int, max_results: int) -> dict[str, Any]:
            p = {**base_params, "startAt": start_at, "maxResults": max_results}
            result: dict[str, Any] = self.get(path, params=p).json()
            return result

        pages = paginate_offset(fetch, max_results_per_page=max_results_per_page, limit=limit, items_key=items_key)
        return collect_all(pages, items_key=items_key)

    def get_paginated_links(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        items_key: str = "results",
        limit: int | None = None,
    ) -> list[Any]:
        base_params = dict(params or {})

        def fetch(next_url: str | None) -> dict[str, Any]:
            url = next_url if next_url else path
            result: dict[str, Any] = self.get(url, params=base_params if not next_url else None).json()
            return result

        pages = paginate_links(fetch, limit=limit)
        return collect_all(pages, items_key=items_key)

    def close(self) -> None:
        self._client.close()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()

    def __enter__(self) -> BaseClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _vlog(message: str) -> None:
    """Emit a verbose line on stderr.

    stderr only: stdout is the machine-readable surface (`--format=json`) and the
    snapshot tests pin it, so verbose output must never reach it.
    """
    print(f"[atls] {message}", file=sys.stderr)


def _render_header(name: str, value: str) -> str:
    lowered = name.lower()
    if lowered in _MASKED_HEADERS:
        return f"{name}: <redacted len={len(value)}>"
    if lowered == "location":
        # Hiding Location would defeat the point of verbose here — a proxy bounce
        # is invisible without it. Scrub the credential-bearing parts instead.
        return f"{name}: {safe_display_url(value)}"
    return f"{name}: {safe_header_value(value, MAX_VERBOSE_HEADER_LEN)}"


def _log_headers(marker: str, headers: Any) -> None:
    for name, value in headers.items():
        _vlog(f"  {marker} {_render_header(str(name), str(value))}")


def _log_proxy_env() -> None:
    def _read(*names: str) -> str:
        for name in names:
            value = os.environ.get(name)
            if value:
                # A proxy URL can embed credentials (http://user:pass@proxy).
                return safe_display_url(value)
        return "(unset)"

    _vlog(f"  env HTTPS_PROXY={_read('HTTPS_PROXY', 'https_proxy')} NO_PROXY={_read('NO_PROXY', 'no_proxy')}")


def _body_summary(response: httpx.Response) -> str:
    """Describe a response body without ever echoing it.

    Bodies carry page content, SSO HTML with embedded tokens, and personal data;
    only shape metadata is safe to print.
    """
    content_type = safe_header_value(response.headers.get("content-type"), 100) or "(none)"
    size = len(response.content)
    summary = f"content-type={content_type} bytes={size}"
    if size > MAX_VERBOSE_JSON_BYTES:
        return f"{summary} (keys omitted: over {MAX_VERBOSE_JSON_BYTES // 1024}KB)"
    if "json" not in content_type.lower():
        return summary
    try:
        parsed = response.json()
    except Exception:
        return f"{summary} (unparseable json)"
    if not isinstance(parsed, dict):
        return f"{summary} json={type(parsed).__name__}"
    names = [safe_header_value(str(key), MAX_VERBOSE_KEY_LEN) for key in list(parsed)[:MAX_VERBOSE_JSON_KEYS]]
    remaining = len(parsed) - len(names)
    rendered = ", ".join(names) + (f", ... (+{remaining} more)" if remaining > 0 else "")
    return f"{summary} keys=[{rendered}]"


def _retry_wait(response: httpx.Response, default_delay: float) -> float:
    """Return seconds to wait before retry, honouring Retry-After if present."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            # Cap retry-after to prevent DoS via malicious header
            return min(float(retry_after), 30.0)
        except ValueError:
            pass
    return default_delay


def _warn_retry(attempt: int, max_retries: int, wait: float, status: int) -> None:
    label = "rate-limited" if status == 429 else f"server error {status}"
    print(f"[atls] retry {attempt}/{max_retries} after {wait:.1f}s ({status} {label})", file=sys.stderr)
