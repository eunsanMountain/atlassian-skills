from __future__ import annotations

import pytest

from atlassian_skills.core.pagination import collect_all, paginate_links, paginate_offset

# ---------------------------------------------------------------------------
# paginate_offset
# ---------------------------------------------------------------------------


def test_offset_three_pages() -> None:
    """total=7, page_size=3 → 3 pages (3+3+1 items)."""
    calls: list[tuple[int, int]] = []

    def fetch(start_at: int, max_results: int) -> dict:
        calls.append((start_at, max_results))
        batch = list(range(start_at, min(start_at + max_results, 7)))
        return {"startAt": start_at, "maxResults": max_results, "total": 7, "issues": batch}

    pages = list(paginate_offset(fetch, max_results_per_page=3))
    assert len(pages) == 3
    assert calls == [(0, 3), (3, 3), (6, 3)]
    all_items = collect_all(iter(pages), items_key="issues")
    assert all_items == list(range(7))


def test_offset_single_page() -> None:
    """total=2, page_size=50 → 1 page."""

    def fetch(start_at: int, max_results: int) -> dict:
        return {"startAt": 0, "maxResults": 50, "total": 2, "issues": [0, 1]}

    pages = list(paginate_offset(fetch, max_results_per_page=50))
    assert len(pages) == 1


def test_offset_empty() -> None:
    """total=0 → 1 page (empty), loop terminates."""

    def fetch(start_at: int, max_results: int) -> dict:
        return {"startAt": 0, "maxResults": 50, "total": 0, "issues": []}

    pages = list(paginate_offset(fetch, max_results_per_page=50))
    assert len(pages) == 1
    assert collect_all(iter(pages), items_key="issues") == []


def test_offset_limit_stops_early() -> None:
    """limit=5 stops after collecting 5 items even if total is larger."""
    calls: list[tuple[int, int]] = []

    def fetch(start_at: int, max_results: int) -> dict:
        calls.append((start_at, max_results))
        batch = list(range(start_at, start_at + max_results))
        return {"startAt": start_at, "maxResults": max_results, "total": 100, "issues": batch}

    pages = list(paginate_offset(fetch, max_results_per_page=3, limit=5))
    # page 1: 3 items, page 2: 2 items → stops at 5
    assert len(pages) == 2
    assert calls == [(0, 3), (3, 2)]
    all_items = collect_all(iter(pages), items_key="issues")
    assert len(all_items) == 5


# ---------------------------------------------------------------------------
# paginate_links
# ---------------------------------------------------------------------------


def _make_link_fetch(num_pages: int, page_size: int = 2) -> object:
    """Return a fetch callable that simulates `num_pages` pages."""

    def fetch(next_url: str | None) -> dict:
        page_index = 0 if next_url is None else int(next_url.split("page=")[1])
        start = page_index * page_size
        results = list(range(start, start + page_size))
        has_next = page_index < num_pages - 1
        links = {"next": f"/rest/api/2/search?page={page_index + 1}"} if has_next else {}
        return {"results": results, "_links": links}

    return fetch


def test_links_three_pages() -> None:
    fetch = _make_link_fetch(num_pages=3, page_size=2)
    pages = list(paginate_links(fetch))  # type: ignore[arg-type]
    assert len(pages) == 3
    all_items = collect_all(iter(pages))
    assert all_items == list(range(6))


def test_links_single_page() -> None:
    def fetch(next_url: str | None) -> dict:
        return {"results": [1, 2], "_links": {}}

    pages = list(paginate_links(fetch))
    assert len(pages) == 1


def test_links_empty_first_page() -> None:
    def fetch(next_url: str | None) -> dict:
        return {"results": [], "_links": {}}

    pages = list(paginate_links(fetch))
    assert len(pages) == 1
    assert collect_all(iter(pages)) == []


def test_links_limit_stops_early() -> None:
    fetch = _make_link_fetch(num_pages=5, page_size=3)
    pages = list(paginate_links(fetch, limit=5))  # type: ignore[arg-type]
    # page 1: 3 items (collected=3), page 2: 3 items (collected=6 >= 5) → stops
    assert len(pages) == 2
    all_items = collect_all(iter(pages))
    assert len(all_items) == 6  # collected items in yielded pages (limit is a soft stop)


# ---------------------------------------------------------------------------
# collect_all
# ---------------------------------------------------------------------------


def test_collect_all_merges_pages() -> None:
    pages = iter(
        [
            {"results": [1, 2]},
            {"results": [3, 4]},
            {"results": [5]},
        ]
    )
    assert collect_all(pages) == [1, 2, 3, 4, 5]


def test_collect_all_custom_key() -> None:
    pages = iter(
        [
            {"issues": ["A", "B"]},
            {"issues": ["C"]},
        ]
    )
    assert collect_all(pages, items_key="issues") == ["A", "B", "C"]


def test_collect_all_empty_pages() -> None:
    pages = iter([{"results": []}, {"results": []}])
    assert collect_all(pages) == []


# ---------------------------------------------------------------------------
# paginate_offset — new edge cases
# ---------------------------------------------------------------------------


def test_paginate_offset_total_zero() -> None:
    """total=0 → 1 page yielded then empty-items guard breaks."""
    call_count = 0

    def fetch(start_at: int, max_results: int) -> dict:
        nonlocal call_count
        call_count += 1
        return {"startAt": 0, "maxResults": max_results, "total": 0, "issues": []}

    pages = list(paginate_offset(fetch))
    assert len(pages) == 1
    assert call_count == 1
    assert collect_all(iter(pages), items_key="issues") == []


def test_paginate_offset_total_negative() -> None:
    """total=-1 → start_at(N) >= -1 is True after first page → breaks after first page."""
    call_count = 0

    def fetch(start_at: int, max_results: int) -> dict:
        nonlocal call_count
        call_count += 1
        return {"startAt": start_at, "maxResults": max_results, "total": -1, "issues": [1, 2]}

    pages = list(paginate_offset(fetch, max_results_per_page=2))
    # start_at becomes 2 after first page; 2 >= -1 → loop exits
    assert len(pages) == 1
    assert call_count == 1


def test_paginate_offset_inflated_total_empty_guard() -> None:
    """total=9999 but page 2 returns empty items → empty-items guard stops iteration."""
    calls: list[tuple[int, int]] = []

    def fetch(start_at: int, max_results: int) -> dict:
        calls.append((start_at, max_results))
        if start_at == 0:
            return {"startAt": 0, "maxResults": max_results, "total": 9999, "issues": [1, 2, 3]}
        return {"startAt": start_at, "maxResults": max_results, "total": 9999, "issues": []}

    pages = list(paginate_offset(fetch, max_results_per_page=3))
    assert len(pages) == 2
    assert calls == [(0, 3), (3, 3)]


def test_paginate_offset_last_page_partial() -> None:
    """Last page has fewer items than page_size → yields 3 pages (5+5+2)."""
    calls: list[tuple[int, int]] = []

    def fetch(start_at: int, max_results: int) -> dict:
        calls.append((start_at, max_results))
        all_items = list(range(12))
        batch = all_items[start_at : start_at + max_results]
        return {"startAt": start_at, "maxResults": max_results, "total": 12, "issues": batch}

    pages = list(paginate_offset(fetch, max_results_per_page=5))
    assert len(pages) == 3
    assert calls == [(0, 5), (5, 5), (10, 5)]
    all_items = collect_all(iter(pages), items_key="issues")
    assert all_items == list(range(12))


def test_paginate_offset_limit_partial_page() -> None:
    """limit=7 with page_size=5: page1=5 items, page2 asks for min(5,2)=2 items → stops."""
    calls: list[tuple[int, int]] = []

    def fetch(start_at: int, max_results: int) -> dict:
        calls.append((start_at, max_results))
        batch = list(range(start_at, start_at + max_results))
        return {"startAt": start_at, "maxResults": max_results, "total": 100, "issues": batch}

    pages = list(paginate_offset(fetch, max_results_per_page=5, limit=7))
    assert len(pages) == 2
    assert calls == [(0, 5), (5, 2)]
    all_items = collect_all(iter(pages), items_key="issues")
    assert len(all_items) == 7


def test_paginate_offset_limit_exact_boundary() -> None:
    """limit == items in first page → stops after exactly 1 page."""
    calls: list[tuple[int, int]] = []

    def fetch(start_at: int, max_results: int) -> dict:
        calls.append((start_at, max_results))
        batch = list(range(start_at, start_at + max_results))
        return {"startAt": start_at, "maxResults": max_results, "total": 100, "issues": batch}

    pages = list(paginate_offset(fetch, max_results_per_page=10, limit=10))
    assert len(pages) == 1
    assert calls == [(0, 10)]
    all_items = collect_all(iter(pages), items_key="issues")
    assert len(all_items) == 10


def test_paginate_offset_custom_items_key() -> None:
    """items_key='values' is respected for both empty-guard and collect_all."""
    calls: list[tuple[int, int]] = []

    def fetch(start_at: int, max_results: int) -> dict:
        calls.append((start_at, max_results))
        all_data = list(range(4))
        batch = all_data[start_at : start_at + max_results]
        return {"startAt": start_at, "maxResults": max_results, "total": 4, "values": batch}

    pages = list(paginate_offset(fetch, max_results_per_page=2, items_key="values"))
    assert len(pages) == 2
    assert calls == [(0, 2), (2, 2)]
    assert collect_all(iter(pages), items_key="values") == list(range(4))


def test_paginate_offset_single_item_pages() -> None:
    """page_size=1, 3 items → exactly 3 pages."""
    calls: list[tuple[int, int]] = []

    def fetch(start_at: int, max_results: int) -> dict:
        calls.append((start_at, max_results))
        all_data = ["a", "b", "c"]
        batch = all_data[start_at : start_at + max_results]
        return {"startAt": start_at, "maxResults": max_results, "total": 3, "issues": batch}

    pages = list(paginate_offset(fetch, max_results_per_page=1))
    assert len(pages) == 3
    assert calls == [(0, 1), (1, 1), (2, 1)]
    assert collect_all(iter(pages), items_key="issues") == ["a", "b", "c"]


def test_paginate_offset_large_limit_small_total() -> None:
    """limit=1000 but total=5 → only 1 page, limit has no additional effect."""
    calls: list[tuple[int, int]] = []

    def fetch(start_at: int, max_results: int) -> dict:
        calls.append((start_at, max_results))
        return {"startAt": 0, "maxResults": max_results, "total": 5, "issues": list(range(5))}

    pages = list(paginate_offset(fetch, max_results_per_page=50, limit=1000))
    assert len(pages) == 1
    assert len(calls) == 1
    assert collect_all(iter(pages), items_key="issues") == list(range(5))


def test_paginate_offset_zero_limit_treated_as_no_limit() -> None:
    """limit=None (falsy) means no cap — exhausts total normally."""
    calls: list[tuple[int, int]] = []

    def fetch(start_at: int, max_results: int) -> dict:
        calls.append((start_at, max_results))
        all_data = list(range(6))
        batch = all_data[start_at : start_at + max_results]
        return {"startAt": start_at, "maxResults": max_results, "total": 6, "issues": batch}

    pages = list(paginate_offset(fetch, max_results_per_page=3, limit=None))
    assert len(pages) == 2
    assert calls == [(0, 3), (3, 3)]


@pytest.mark.parametrize(
    "total,page_size,expected_pages",
    [
        (0, 50, 1),
        (1, 50, 1),
        (50, 50, 1),
        (51, 50, 2),
        (100, 50, 2),
        (101, 50, 3),
    ],
)
def test_paginate_offset_page_count_matrix(total: int, page_size: int, expected_pages: int) -> None:
    """Matrix: verifies correct page count for various total/page_size combos."""
    pages_seen: list[dict] = []

    def fetch(start_at: int, max_results: int) -> dict:
        all_data = list(range(total))
        batch = all_data[start_at : start_at + max_results]
        return {"startAt": start_at, "maxResults": max_results, "total": total, "issues": batch}

    pages_seen = list(paginate_offset(fetch, max_results_per_page=page_size))
    assert len(pages_seen) == expected_pages


# ---------------------------------------------------------------------------
# paginate_links — new edge cases
# ---------------------------------------------------------------------------


def test_paginate_links_no_next_first_page() -> None:
    """First page has no _links.next → stops after 1 page."""
    call_count = 0

    def fetch(next_url: str | None) -> dict:
        nonlocal call_count
        call_count += 1
        return {"results": [{"id": 1}, {"id": 2}], "_links": {}}

    pages = list(paginate_links(fetch))
    assert len(pages) == 1
    assert call_count == 1


def test_paginate_links_empty_results_no_next() -> None:
    """results=[] and no _links.next → stops after 1 page."""

    def fetch(next_url: str | None) -> dict:
        return {"results": [], "_links": {}}

    pages = list(paginate_links(fetch))
    assert len(pages) == 1
    assert collect_all(iter(pages)) == []


def test_paginate_links_limit_mid_page() -> None:
    """limit reached mid-pagination: 3 pages available but limit=4 stops at page 2."""
    fetch = _make_link_fetch(num_pages=3, page_size=3)
    # page1: 3 items (collected=3), page2: 3 items (collected=6 >= 4) → stops after page 2
    pages = list(paginate_links(fetch, limit=4))  # type: ignore[arg-type]
    assert len(pages) == 2


def test_paginate_links_three_pages() -> None:
    """Explicit 3-page traversal with distinct URLs and items."""
    urls_seen: list[str | None] = []

    def fetch(next_url: str | None) -> dict:
        urls_seen.append(next_url)
        page_map = {
            None: {"results": [1, 2], "_links": {"next": "/page2"}},
            "/page2": {"results": [3, 4], "_links": {"next": "/page3"}},
            "/page3": {"results": [5], "_links": {}},
        }
        return page_map[next_url]

    pages = list(paginate_links(fetch))
    assert len(pages) == 3
    assert urls_seen == [None, "/page2", "/page3"]
    assert collect_all(iter(pages)) == [1, 2, 3, 4, 5]


def test_paginate_links_limit_exact_match() -> None:
    """collected == limit exactly stops pagination without fetching the next page."""
    fetch = _make_link_fetch(num_pages=5, page_size=2)
    # page1: 2 items (collected=2 >= 2) → stops
    pages = list(paginate_links(fetch, limit=2))  # type: ignore[arg-type]
    assert len(pages) == 1
    assert collect_all(iter(pages)) == [0, 1]


def test_paginate_links_single_result_per_page() -> None:
    """1 result per page, 4 pages → 4 pages yielded."""
    page_index = [0]

    def fetch(next_url: str | None) -> dict:
        idx = page_index[0]
        page_index[0] += 1
        has_next = idx < 3
        return {
            "results": [idx],
            "_links": {"next": f"/page{idx + 1}"} if has_next else {},
        }

    pages = list(paginate_links(fetch))
    assert len(pages) == 4
    assert collect_all(iter(pages)) == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# collect_all — new edge cases
# ---------------------------------------------------------------------------


def test_collect_all_custom_items_key() -> None:
    """items_key='values' collects correctly."""
    pages = iter(
        [
            {"values": [10, 20]},
            {"values": [30]},
        ]
    )
    assert collect_all(pages, items_key="values") == [10, 20, 30]


def test_collect_all_all_empty_pages() -> None:
    """All pages have empty results → returns empty list."""
    pages = iter(
        [
            {"results": []},
            {"results": []},
            {"results": []},
        ]
    )
    assert collect_all(pages) == []


def test_collect_all_mixed_page_sizes() -> None:
    """Pages with 3, 1, 5 items are all flattened correctly."""
    pages = iter(
        [
            {"results": [1, 2, 3]},
            {"results": [4]},
            {"results": [5, 6, 7, 8, 9]},
        ]
    )
    assert collect_all(pages) == list(range(1, 10))


def test_collect_all_default_key() -> None:
    """Default items_key is 'results'."""
    pages = iter(
        [
            {"results": ["x", "y"]},
            {"results": ["z"]},
        ]
    )
    assert collect_all(pages) == ["x", "y", "z"]
