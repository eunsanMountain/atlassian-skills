from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any


def paginate_offset(
    fetch_fn: Callable[[int, int], dict[str, Any]],
    max_results_per_page: int = 50,
    limit: int | None = None,
    items_key: str = "issues",
) -> Iterator[dict[str, Any]]:
    """Yield pages. fetch_fn(start_at, max_results) → response dict with 'total', 'startAt', 'maxResults'."""
    start_at = 0
    collected = 0
    while True:
        page_size = min(max_results_per_page, limit - collected) if limit else max_results_per_page
        response = fetch_fn(start_at, page_size)
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
) -> Iterator[dict[str, Any]]:
    """Yield pages. fetch_fn(next_url_or_none) → response dict with '_links.next' if more pages."""
    next_url = None
    collected = 0
    while True:
        response = fetch_fn(next_url)
        yield response
        results = response.get("results", [])
        collected += len(results)
        if limit and collected >= limit:
            break
        links = response.get("_links", {})
        next_url = links.get("next")
        if not next_url:
            break


def collect_all(pages: Iterator[dict[str, Any]], items_key: str = "results") -> list[Any]:
    """Flatten paginated results into a single list."""
    items = []
    for page in pages:
        items.extend(page.get(items_key, []))
    return items
