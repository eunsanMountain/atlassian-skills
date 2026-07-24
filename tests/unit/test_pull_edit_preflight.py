"""Pull-time in-place editability prediction (no false-clear pages).

Failure mode: a page could pull clean ("Edit normally...") and only then
fail the push dry-run fail-closed. The pull receipt now runs the managed
proof once on the unedited round-trip and grades edit_guidance as
in_place_blocked / in_place_with_consent / (existing kinds when ready), so a
user learns *before editing* what this page supports.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atlassian_skills.confluence.managed_pull import (
    _predict_in_place_editability,
    prepare_portable_pull,
)
from atlassian_skills.confluence.models import Page

PLAIN = "<p>alpha paragraph text here</p><p>beta paragraph text here</p>"
CONSENT = (
    '<ul><li data-uuid="00000000-0000-0000-0000-000000000001">'
    "<p>alpha bravo charlie delta echo</p></li></ul><p>closing paragraph text</p>"
)
# A genuinely ambiguous page: the emoticon conversion and the trailing-space
# trim produce the identical tail deletion, so ownership stays unresolvable
# (multiple-change-owners). The former fixture — a decorated table — now
# proves clean through the ordered-composition witness.
BLOCKED = '<p>prefix<ac:emoticon ac:name="smile"/>   </p><p>closing paragraph text</p>'


class FakeClient:
    base_url = "https://example.invalid"

    def __init__(self, storage: str) -> None:
        self.storage = storage

    def get_page(self, page_id: str, *args, **kwargs) -> Page:
        return Page.model_validate(
            {
                "id": "1",
                "title": "fixture-page",
                "type": "page",
                "status": "current",
                "space": {"key": "FIX", "name": "Fixture"},
                "version": {"number": 1},
                "body": {"storage": {"value": self.storage}},
            }
        )

    def list_attachments(self, page_id: str, limit=None):
        return []


def _pull_guidance(storage: str, tmp_path: Path) -> tuple[str, ...]:
    prepared = prepare_portable_pull(FakeClient(storage), "1", tmp_path / "page.md", site_url="https://example.invalid")
    return tuple(entry["kind"] for entry in prepared.edit_guidance)


def test_predictor_grades_the_three_classes() -> None:
    import cfxmark

    options = cfxmark.ConversionOptions(profile="editable")
    for storage, expected in ((PLAIN, "ready"), (CONSENT, "consent"), (BLOCKED, "blocked")):
        artifact = cfxmark.to_md_artifact(storage, options=options)
        kind, codes = _predict_in_place_editability(artifact, storage, ())
        assert kind == expected, (expected, kind, codes)


def test_blocked_page_pull_says_so_before_any_edit(tmp_path: Path) -> None:
    kinds = _pull_guidance(BLOCKED, tmp_path)
    assert kinds == ("in_place_blocked",)


def test_consent_page_pull_names_the_losses(tmp_path: Path) -> None:
    prepared = prepare_portable_pull(FakeClient(CONSENT), "1", tmp_path / "page.md", site_url="https://example.invalid")
    (entry,) = prepared.edit_guidance
    assert entry["kind"] == "in_place_with_consent"
    assert "element-attribute-omitted" in entry["codes"]


def test_ready_page_keeps_existing_guidance(tmp_path: Path) -> None:
    kinds = _pull_guidance(PLAIN, tmp_path)
    assert kinds == ("proof_candidate",)


def test_unexpected_predictor_failure_still_pulls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The prediction is advisory: an unexpected failure must not abort the pull.

    The predictor now runs the full ownership proof *before* the file is written,
    so an exception type outside its caught set would turn a previously working
    pull into a no-output failure. Pull is the recovery path; it degrades to the
    neutral pre-existing guidance instead — never to a false safe or blocked one.
    """
    from atlassian_skills.confluence import managed_pull

    def boom(*_args: object, **_kwargs: object) -> tuple[str, tuple[str, ...]]:
        raise RecursionError("pathological page")

    monkeypatch.setattr(managed_pull, "_predict_in_place_editability", boom)
    prepared = prepare_portable_pull(FakeClient(PLAIN), "1", tmp_path / "page.md", site_url="https://example.invalid")

    # The pull still produces its managed Markdown for the caller to write.
    assert prepared.markdown.strip()
    assert prepared.status in {"pulled", "pulled_with_migrations"}
    assert tuple(entry["kind"] for entry in prepared.edit_guidance) == ("proof_candidate",)
