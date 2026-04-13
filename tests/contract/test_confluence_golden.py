from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from atlassian_skills.confluence.models import (
    Comment,
    ConfluenceSearchResult,
    Label,
    Page,
    SpaceTreeResult,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "confluence"


def load(filename: str) -> object:
    return json.loads((FIXTURES / filename).read_text())


# ---------------------------------------------------------------------------
# get-page-sample.json → Page  (preprocessed fixture: wrapped in 'metadata')
# ---------------------------------------------------------------------------


class TestGoldenPage:
    def test_golden_page_parses(self) -> None:
        data = load("get-page-sample.json")
        # Fixture wraps page data under 'metadata' key
        page = Page.model_validate(data["metadata"])
        assert page.id == "429140627"
        assert page.title

    def test_golden_page_body_storage_extracted(self) -> None:
        data = load("get-page-sample.json")
        page = Page.model_validate(data["metadata"])
        # body_storage may be None when 'body' envelope is absent;
        # the fixture stores content under 'content', not 'body'
        assert page.content is not None
        assert isinstance(page.content.value, str)
        assert len(page.content.value) > 0

    def test_golden_page_space_info(self) -> None:
        data = load("get-page-sample.json")
        page = Page.model_validate(data["metadata"])
        assert page.space is not None
        assert page.space.key == "TESTSPACE"
        assert page.space.name == "Test Lab"

    def test_golden_page_version(self) -> None:
        data = load("get-page-sample.json")
        page = Page.model_validate(data["metadata"])
        # version is stored as plain int in this fixture
        assert page.version is not None
        assert page.version == 2

    def test_golden_page_type(self) -> None:
        data = load("get-page-sample.json")
        page = Page.model_validate(data["metadata"])
        assert page.type == "page"

    def test_golden_page_url_accessible(self) -> None:
        data = load("get-page-sample.json")
        page = Page.model_validate(data["metadata"])
        assert page.url is not None
        assert "429140627" in page.url


# ---------------------------------------------------------------------------
# search-proj.json → ConfluenceSearchResult  (fixture is a raw list of Pages)
# ---------------------------------------------------------------------------


class TestGoldenConfluenceSearch:
    def test_golden_search_parses(self) -> None:
        raw = load("search-proj.json")
        # Fixture is a list of page objects; wrap into ConfluenceSearchResult shape
        assert isinstance(raw, list)
        result = ConfluenceSearchResult.model_validate({"results": raw, "total": len(raw)})
        assert len(result.results) == 3

    def test_golden_search_results_count(self) -> None:
        raw = load("search-proj.json")
        result = ConfluenceSearchResult.model_validate({"results": raw, "total": len(raw)})
        assert result.total == 3

    def test_golden_search_each_result_has_id_and_title(self) -> None:
        raw = load("search-proj.json")
        result = ConfluenceSearchResult.model_validate({"results": raw, "total": len(raw)})
        for page in result.results:
            assert page.id
            assert page.title

    def test_golden_search_space_info(self) -> None:
        raw = load("search-proj.json")
        result = ConfluenceSearchResult.model_validate({"results": raw, "total": len(raw)})
        for page in result.results:
            assert page.space is not None
            assert page.space.key == "TESTSPACE"


# ---------------------------------------------------------------------------
# get-page-history-v1.json — preprocessed Page (version 1)
# ---------------------------------------------------------------------------


class TestGoldenPageHistory:
    def test_golden_history_parses(self) -> None:
        data = load("get-page-history-v1.json")
        page = Page.model_validate(data)
        assert page.id == "429140627"
        assert page.title

    def test_golden_history_version_is_1(self) -> None:
        data = load("get-page-history-v1.json")
        page = Page.model_validate(data)
        assert page.version == 1

    def test_golden_history_space_key(self) -> None:
        data = load("get-page-history-v1.json")
        page = Page.model_validate(data)
        assert page.space is not None
        assert page.space.key == "TESTSPACE"


# ---------------------------------------------------------------------------
# get-comments-sample.json → list[Comment]
# ---------------------------------------------------------------------------


class TestGoldenConfluenceComments:
    def test_golden_comments_parse(self) -> None:
        data = load("get-comments-sample.json")
        adapter: TypeAdapter[list[Comment]] = TypeAdapter(list[Comment])
        comments = adapter.validate_python(data["results"])
        assert len(comments) == 2

    def test_golden_comment_ids(self) -> None:
        data = load("get-comments-sample.json")
        adapter: TypeAdapter[list[Comment]] = TypeAdapter(list[Comment])
        comments = adapter.validate_python(data["results"])
        ids = [c.id for c in comments]
        assert "456789001" in ids
        assert "456789002" in ids

    def test_golden_comment_version_accessible(self) -> None:
        data = load("get-comments-sample.json")
        adapter: TypeAdapter[list[Comment]] = TypeAdapter(list[Comment])
        comments = adapter.validate_python(data["results"])
        first = comments[0]
        assert first.version is not None
        assert first.version.number == 1


# ---------------------------------------------------------------------------
# get-labels-sample.json → list[Label]
# ---------------------------------------------------------------------------


class TestGoldenLabels:
    def test_golden_labels_parse(self) -> None:
        data = load("get-labels-sample.json")
        adapter: TypeAdapter[list[Label]] = TypeAdapter(list[Label])
        labels = adapter.validate_python(data["results"])
        assert len(labels) == 3

    def test_golden_label_names(self) -> None:
        data = load("get-labels-sample.json")
        adapter: TypeAdapter[list[Label]] = TypeAdapter(list[Label])
        labels = adapter.validate_python(data["results"])
        names = [lb.name for lb in labels]
        assert "architecture" in names
        assert "reviewed" in names
        assert "important" in names

    def test_golden_label_prefix(self) -> None:
        data = load("get-labels-sample.json")
        adapter: TypeAdapter[list[Label]] = TypeAdapter(list[Label])
        labels = adapter.validate_python(data["results"])
        arch = next(lb for lb in labels if lb.name == "architecture")
        assert arch.prefix == "global"
        important = next(lb for lb in labels if lb.name == "important")
        assert important.prefix == "my"


# ---------------------------------------------------------------------------
# get-space-tree-sample.json → SpaceTreeResult
# ---------------------------------------------------------------------------


class TestGoldenSpaceTree:
    def test_golden_space_tree_parses(self) -> None:
        data = load("get-space-tree-sample.json")
        result = SpaceTreeResult.model_validate(data)
        assert result.space_key == "TESTSPACE"
        assert result.total_pages == 10

    def test_golden_space_tree_pages_count(self) -> None:
        data = load("get-space-tree-sample.json")
        result = SpaceTreeResult.model_validate(data)
        assert len(result.pages) == 10

    def test_golden_space_tree_has_more(self) -> None:
        data = load("get-space-tree-sample.json")
        result = SpaceTreeResult.model_validate(data)
        assert result.has_more is True

    def test_golden_space_tree_page_fields(self) -> None:
        data = load("get-space-tree-sample.json")
        result = SpaceTreeResult.model_validate(data)
        first = result.pages[0]
        assert first.id == "191410941"
        assert first.title
        assert first.depth == 6
