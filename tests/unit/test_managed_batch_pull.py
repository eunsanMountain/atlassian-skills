from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from atlassian_skills.confluence.pull_md import (
    pull_pages_batch,
    safe_page_directory_name,
)
from atlassian_skills.core.attachment_io import safe_attachment_filename
from atlassian_skills.core.errors import ConflictError
from atlassian_skills.core.managed_manifest import parse_managed_document


class FakeClient:
    base_url = "https://example.com/confluence"

    def __init__(self) -> None:
        self.pages = {
            "100": SimpleNamespace(
                id="100", title="Page One", body_storage="<p>One</p>", version=SimpleNamespace(number=3)
            ),
            "200": SimpleNamespace(
                id="200", title="Page Two", body_storage="<p>Two</p>", version=SimpleNamespace(number=4)
            ),
        }

    def get_page(self, page_id: str) -> SimpleNamespace:
        return self.pages[page_id]


def _page_path(root: Path, page_id: str, title: str) -> Path:
    directory = root / safe_page_directory_name(title, page_id)
    return directory / f"{safe_attachment_filename(title, f'page_{page_id}')[:120]}.md"


def test_portable_batch_preflights_every_output_before_publication(tmp_path: Path) -> None:
    root = tmp_path / "pages"
    conflict = _page_path(root, "200", "Page Two")
    conflict.parent.mkdir(parents=True)
    conflict.write_text("user-owned\n", encoding="utf-8")

    with pytest.raises(ConflictError) as exc_info:
        pull_pages_batch(FakeClient(), ["100", "200"], root, portable=True)

    assert exc_info.value.context["reason"] == "output_conflict"
    assert not _page_path(root, "100", "Page One").exists()
    assert conflict.read_text(encoding="utf-8") == "user-owned\n"


def test_portable_batch_uses_same_state_free_manifest_contract(tmp_path: Path) -> None:
    root = tmp_path / "pages"

    results = pull_pages_batch(FakeClient(), ["100", "200"], root, portable=True, no_assets=True)

    assert [result.status for result in results] == ["pulled", "pulled"]
    for result in results:
        parsed = parse_managed_document(result.path.read_text(encoding="utf-8"))
        assert parsed.manifest.page == result.page_id
