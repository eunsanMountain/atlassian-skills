from __future__ import annotations

import contextlib
import sys
import time
from typing import Any

import httpx

from atlassian_skills.core.auth import Credential
from atlassian_skills.core.errors import NetworkError, http_error_to_atlas
from atlassian_skills.core.pagination import collect_all, paginate_links, paginate_offset

_RETRY_STATUSES = {429, 500, 502, 503, 504}


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
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        url = path if path.startswith(("http://", "https://")) else f"{self.base_url}/{path.lstrip('/')}"
        merged_headers = {**self.credential.to_header(), **(headers or {})}

        attempt = 0
        delay = 1.0
        total_start = time.monotonic()
        max_total_retry_seconds = 90.0

        while True:
            try:
                response = self._client.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    data=data,
                    headers=merged_headers,
                )
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

            if response.status_code in _RETRY_STATUSES and attempt < self.max_retries:
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

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        return self.request("GET", path, params=params)

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

    def delete(self, path: str) -> httpx.Response:
        return self.request("DELETE", path)

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
