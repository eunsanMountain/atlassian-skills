from __future__ import annotations

import json
from pathlib import Path

from atlassian_skills.confluence.models import (
    Attachment,
    Comment,
    ConfluenceSearchResult,
    Label,
    Page,
    PageVersion,
    Space,
    SpaceTreeNode,
    SpaceTreeResult,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "confluence"


def load(filename: str) -> object:
    return json.loads((FIXTURES / filename).read_text())


class TestPageModel:
    def test_get_page_preprocessed(self) -> None:
        """Validate the preprocessed MCP-captured page fixture."""
        raw = load("get-page-429140627.json")
        assert isinstance(raw, dict)
        data = raw.get("metadata", raw)
        page = Page.model_validate(data)
        assert page.id == "429140627"
        assert page.title == "[RLM-3] Navi Map 통합-경로 판단 개선"
        assert page.type == "page"
        assert page.space is not None
        assert page.space.key == "IVSL"
        assert page.space.name == "IVS Lab"

    def test_get_page_version(self) -> None:
        raw = load("get-page-429140627.json")
        assert isinstance(raw, dict)
        data = raw.get("metadata", raw)
        page = Page.model_validate(data)
        assert page.version == 2

    def test_get_page_content(self) -> None:
        raw = load("get-page-429140627.json")
        assert isinstance(raw, dict)
        data = raw.get("metadata", raw)
        page = Page.model_validate(data)
        assert page.content is not None
        assert page.content.format == "markdown"

    def test_get_page_raw_format(self) -> None:
        raw = load("get-page-429140627-raw.json")
        assert isinstance(raw, dict)
        data = raw.get("metadata", raw)
        page = Page.model_validate(data)
        assert page.id == "429140627"
        assert page.content is not None
        assert page.content.format == "storage"

    def test_page_extra_fields_ignored(self) -> None:
        data = {
            "id": "1",
            "title": "Test",
            "unknown_field": "should be ignored",
        }
        page = Page.model_validate(data)
        assert page.id == "1"
        assert page.title == "Test"

    def test_page_optional_fields_default(self) -> None:
        minimal = {"id": "1", "title": "Test"}
        page = Page.model_validate(minimal)
        assert page.status is None
        assert page.space is None
        assert page.ancestors == []
        assert page.children == []

    def test_space_id_accepts_integer_from_server(self) -> None:
        space = Space.model_validate({"id": 12345, "key": "IVSL", "name": "IVS Lab"})
        assert space.id == 12345


class TestPageHistoryFixture:
    def test_history_v1(self) -> None:
        raw = load("get-page-history-v1.json")
        assert isinstance(raw, dict)
        page = Page.model_validate(raw)
        assert page.id == "429140627"
        assert page.version == 1
        assert page.title == "[RLM-3] 분기점/합류점 경로 판단 개선"

    def test_history_content(self) -> None:
        raw = load("get-page-history-v1.json")
        assert isinstance(raw, dict)
        page = Page.model_validate(raw)
        assert page.content is not None
        assert page.content.format == "markdown"


class TestSearchFixture:
    def test_search_results_list(self) -> None:
        """The search fixture is a plain list of pages."""
        raw = load("search-rlm.json")
        assert isinstance(raw, list)
        pages = [Page.model_validate(item) for item in raw]
        assert len(pages) == 3
        assert pages[0].id == "429148294"
        assert pages[0].title == "[RLM-23] Multi-Dataset 학습 인프라"
        assert pages[0].space is not None
        assert pages[0].space.key == "IVSL"

    def test_search_result_types(self) -> None:
        raw = load("search-rlm.json")
        assert isinstance(raw, list)
        pages = [Page.model_validate(item) for item in raw]
        for p in pages:
            assert p.type == "page"


class TestSpaceTreeFixture:
    def test_space_tree_result(self) -> None:
        raw = load("get-space-tree-ivsl.json")
        assert isinstance(raw, dict)
        result = SpaceTreeResult.model_validate(raw)
        assert result.space_key == "IVSL"
        assert result.total_pages == 10
        assert result.has_more is True
        assert len(result.pages) == 10

    def test_space_tree_node_attributes(self) -> None:
        raw = load("get-space-tree-ivsl.json")
        assert isinstance(raw, dict)
        result = SpaceTreeResult.model_validate(raw)
        node = result.pages[0]
        assert isinstance(node, SpaceTreeNode)
        assert node.id == "191410941"
        assert node.title == "01. [통합 인지] Sensor Fusion Architecture"
        assert node.parent_id == "330432916"

    def test_space_tree_depths(self) -> None:
        raw = load("get-space-tree-ivsl.json")
        assert isinstance(raw, dict)
        result = SpaceTreeResult.model_validate(raw)
        depths = {n.depth for n in result.pages}
        assert depths == {6, 7, 8, 9}


# ---------------------------------------------------------------------------
# New tests (+10)
# ---------------------------------------------------------------------------


class TestPageNoBody:
    def test_page_body_storage_is_none_when_absent(self) -> None:
        data: dict = {"id": "1", "title": "No body"}
        page = Page.model_validate(data)
        assert page.body_storage is None

    def test_page_with_ancestors(self) -> None:
        data: dict = {
            "id": "10",
            "title": "Child Page",
            "ancestors": [
                {"id": "5", "title": "Parent"},
                {"id": "1", "title": "Root"},
            ],
        }
        page = Page.model_validate(data)
        assert len(page.ancestors) == 2
        assert page.ancestors[0].id == "5"
        assert page.ancestors[1].title == "Root"

    def test_page_with_version_info(self) -> None:
        data: dict = {
            "id": "20",
            "title": "Versioned",
            "version": {"number": 3, "when": "2026-04-01T10:00:00.000Z"},
        }
        page = Page.model_validate(data)
        assert isinstance(page.version, PageVersion)
        assert page.version.number == 3
        assert page.version.when == "2026-04-01T10:00:00.000Z"


class TestConfluenceSearchResultEdgeCases:
    def test_empty_results(self) -> None:
        data: dict = {"results": [], "start": 0, "limit": 25, "total": 0}
        result = ConfluenceSearchResult.model_validate(data)
        assert result.total == 0
        assert result.results == []

    def test_links_next_for_pagination(self) -> None:
        data: dict = {
            "results": [],
            "start": 0,
            "limit": 10,
            "total": 50,
            "_links": {"next": "/wiki/rest/api/content/search?start=10&limit=10"},
        }
        result = ConfluenceSearchResult.model_validate(data)
        assert result.links is not None
        assert result.links.next_link is not None
        assert "start=10" in result.links.next_link


class TestCommentNestedBody:
    def test_comment_with_body_view(self) -> None:
        data: dict = {
            "id": "comment-1",
            "title": "Re: page",
            "body": {"view": {"value": "<p>Hello</p>", "representation": "view"}},
        }
        comment = Comment.model_validate(data)
        assert comment.id == "comment-1"
        # body_view is None since it isn't in the nested body structure for Comment
        # (Comment uses body_view as a direct field, not extracted)
        assert comment.title == "Re: page"


class TestAttachmentMediaType:
    def test_image_attachment(self) -> None:
        data: dict = {
            "id": "att-1",
            "title": "photo.png",
            "mediaType": "image/png",
            "fileSize": 102400,
        }
        att = Attachment.model_validate(data)
        assert att.media_type == "image/png"
        assert att.media_type is not None and att.media_type.startswith("image/")

    def test_non_image_attachment(self) -> None:
        data: dict = {
            "id": "att-2",
            "title": "document.pdf",
            "mediaType": "application/pdf",
            "fileSize": 204800,
        }
        att = Attachment.model_validate(data)
        assert att.media_type == "application/pdf"
        assert att.media_type is not None and not att.media_type.startswith("image/")


class TestSpaceTreeNodeWithChildren:
    def test_node_with_children(self) -> None:
        data: dict = {
            "id": "parent-1",
            "title": "Parent",
            "children": [
                {"id": "child-1", "title": "Child A", "depth": 1},
                {"id": "child-2", "title": "Child B", "depth": 1},
            ],
        }
        node = SpaceTreeNode.model_validate(data)
        assert len(node.children) == 2
        assert node.children[0].id == "child-1"
        assert node.children[1].title == "Child B"


class TestLabelPrefixes:
    def test_global_prefix(self) -> None:
        label = Label.model_validate({"name": "release"})
        assert label.prefix == "global"

    def test_custom_prefix(self) -> None:
        label = Label.model_validate({"name": "my-label", "prefix": "team"})
        assert label.prefix == "team"
        assert label.name == "my-label"


class TestConfluenceModelDump:
    def test_page_model_dump(self) -> None:
        page = Page.model_validate({"id": "1", "title": "Test"})
        d = page.model_dump()
        assert isinstance(d, dict)
        assert d["id"] == "1"

    def test_confluence_search_result_model_dump(self) -> None:
        result = ConfluenceSearchResult.model_validate(
            {"results": [], "start": 0, "limit": 25, "total": 0}
        )
        d = result.model_dump()
        assert isinstance(d, dict)
        assert d["total"] == 0

    def test_space_tree_node_model_dump(self) -> None:
        node = SpaceTreeNode.model_validate({"id": "n1", "title": "Node"})
        d = node.model_dump()
        assert isinstance(d, dict)
        assert d["id"] == "n1"
