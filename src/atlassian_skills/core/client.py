from __future__ import annotations

import contextlib
import re
import sys
import time
from typing import Any

import httpx

from atlassian_skills.core.auth import Credential
from atlassian_skills.core.errors import AuthError, NetworkError, ValidationError, http_error_to_atlas
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


def _effective_port(url: httpx.URL) -> int | None:
    if url.port is not None:
        return url.port
    return {"http": 80, "https": 443}.get(url.scheme)


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return url.scheme, url.host, _effective_port(url)


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
        headers=response.headers,
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
        headers=response.headers,
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
        verify: str | bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.credential = credential
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=timeout, verify=verify)

    # ------------------------------------------------------------------
    # Core request with retry
    # ------------------------------------------------------------------

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
                            headers=streamed.headers,
                            content=b"",
                            request=streamed.request,
                            extensions=streamed.extensions,
                        )
                    elif streamed.is_success:
                        response = _read_bounded_response(streamed, response_limit)
                    else:
                        response = _read_error_response(streamed)
            except httpx.TimeoutException as exc:
                raise NetworkError(
                    f"Request timed out: {method} {url}",
                    http_url=url,
                    http_method=method,
                ) from exc
            except httpx.RequestError as exc:
                raise NetworkError(
                    f"Connection error: {exc}",
                    http_url=url,
                    http_method=method,
                ) from exc

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

            # Guarded redirect following (safe methods only)
            if response.status_code in _REDIRECT_STATUSES and method.upper() in ("GET", "HEAD"):
                location = response.headers.get("location")
                redirects += 1
                if location and redirects <= _MAX_REDIRECTS:
                    try:
                        target = httpx.URL(url).join(location)
                    except (httpx.InvalidURL, ValueError) as error:
                        raise ValidationError(
                            "Redirect target URL is malformed",
                            context={"reason": "invalid_redirect_url"},
                        ) from error
                    base = httpx.URL(self.base_url)
                    if target.username or target.password or _origin(target) != _origin(base):
                        raise ValidationError(
                            "Redirect target must stay on the configured Atlassian origin",
                            context={
                                "reason": "unsafe_redirect",
                                "http_status": response.status_code,
                            },
                        )
                    if _LOGIN_PATH_RE.search(target.path):
                        raise AuthError(
                            "Server redirected to a login page; the session or token is not accepted",
                            context={"reason": "redirected_to_login"},
                        )
                    # The Location URL carries its own query; do not re-apply params.
                    url = str(target)
                    params = None
                    continue

            # Non-retryable error
            body: str | None = None
            with contextlib.suppress(Exception):
                body = response.text

            raise http_error_to_atlas(response.status_code, url, method, body)

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
