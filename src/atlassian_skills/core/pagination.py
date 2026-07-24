from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from atlassian_skills.core.errors import ValidationError

DEFAULT_MAX_PAGINATION_PAGES = 10_000


def _validate_max_pages(max_pages: int) -> None:
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")


def paginate_offset(
    fetch_fn: Callable[[int, int], dict[str, Any]],
    max_results_per_page: int = 50,
    limit: int | None = None,
    items_key: str = "issues",
    max_pages: int = DEFAULT_MAX_PAGINATION_PAGES,
) -> Iterator[dict[str, Any]]:
    """Yield pages. fetch_fn(start_at, max_results) → response dict with 'total', 'startAt', 'maxResults'."""
    _validate_max_pages(max_pages)
    start_at = 0
    collected = 0
    page_count = 0
    while True:
        if page_count >= max_pages:
            raise ValidationError(
                "Pagination exceeded the safe page limit",
                context={"reason": "pagination_page_limit", "max_pages": max_pages},
            )
        page_size = min(max_results_per_page, limit - collected) if limit else max_results_per_page
        response = fetch_fn(start_at, page_size)
        page_count += 1
        yield response
        items = response.get(items_key, [])
        if not items:
            break
        total = response.get("total", 0)
        actual = len(items)
        start_at += actual
        collected += actual
        if start_at >= total or (limit and collected >= limit):
            break


def paginate_links(
    fetch_fn: Callable[[str | None], dict[str, Any]],
    limit: int | None = None,
    max_pages: int = DEFAULT_MAX_PAGINATION_PAGES,
) -> Iterator[dict[str, Any]]:
    """Yield pages. fetch_fn(next_url_or_none) → response dict with '_links.next' if more pages."""
    _validate_max_pages(max_pages)
    next_url = None
    collected = 0
    page_count = 0
    seen_urls: set[str] = set()
    while True:
        if page_count >= max_pages:
            raise ValidationError(
                "Pagination exceeded the safe page limit",
                context={"reason": "pagination_page_limit", "max_pages": max_pages},
            )
        if next_url is not None:
            if next_url in seen_urls:
                raise ValidationError(
                    "Pagination returned a repeated next link",
                    context={"reason": "pagination_cycle"},
                )
            seen_urls.add(next_url)
        response = fetch_fn(next_url)
        page_count += 1
        yield response
        results = response.get("results", [])
        collected += len(results)
        if limit and collected >= limit:
            break
        links = response.get("_links", {})
        raw_next = links.get("next")
        if raw_next is not None and not isinstance(raw_next, str):
            raise ValidationError(
                "Pagination returned an invalid next-link token",
                context={"reason": "pagination_token_invalid"},
            )
        next_url = raw_next
        if not next_url:
            break


def collect_all(pages: Iterator[dict[str, Any]], items_key: str = "results") -> list[Any]:
    """Flatten paginated results into a single list."""
    items = []
    for page in pages:
        items.extend(page.get(items_key, []))
    return items
