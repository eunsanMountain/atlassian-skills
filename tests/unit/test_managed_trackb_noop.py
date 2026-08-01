"""W1a Track-B interaction: an unrepresentable non-1 nested ordered list.

Track B (``<ol start!="1">`` nested under a list item) fails closed at cfxmark
``to_cfx`` conversion. These tests pin the three required managed behaviours:

(a) a fresh pull + unedited re-push returns ``no_change`` with no PUT and no consent
    — decided from M0==M1 plus the manifest/remote identity match, WITHOUT requiring
    a clean ``to_cfx(M1)`` round trip (which would fail closed for Track B);
(b) an edit OUTSIDE the Track-B subtree publishes normally and byte-preserves the
    Track-B storage as an exact remote prefix (positive splice preservation);
(c) an edit INSIDE the unrepresentable shape fails closed with no PUT and is a hard
    validation failure, never an ``--accept-migration`` consent prompt.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from atlassian_skills.confluence.migration_preflight import build_managed_preflight
from atlassian_skills.confluence.push_md import push_md
from atlassian_skills.core.errors import MigrationConsentRequiredError, ValidationError
from tests.unit.managed_seam import pull_managed_suspending_the_write_policy
from tests.unit.test_state_free_body_write import BodyClient

# nol_start2_paragraph_shape: a non-1 nested ordered list, Markdown-unrepresentable.
_TRACK_B = '<ul><li>step<ol start="2"><li>b</li></ol></li></ul>'


class ReadOnlyClient:
    """Serves a fixed Track-B body; a PUT would be a bug, so it is not implemented."""

    base_url = "https://example.com/confluence"

    def __init__(self, storage: str) -> None:
        self.storage = storage
        self.version = 7
        self.puts = 0

    def get_page(self, page_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=page_id,
            title="Page",
            body_storage=self.storage,
            version=SimpleNamespace(number=self.version),
        )


def test_trackb_unedited_repush_is_no_change_without_put_or_consent(tmp_path: Path) -> None:
    client = ReadOnlyClient(_TRACK_B)
    path = tmp_path / "page.md"
    pull_managed_suspending_the_write_policy(client, "123", path, no_assets=True)

    # The no-op decision must be reachable without a clean to_cfx(M1): build the
    # preflight (which would otherwise crash at conversion) and require no_change.
    preflight = build_managed_preflight(client, "123", path)
    assert preflight.proof_mode == "no_change"
    assert preflight.status == "no_change"
    assert preflight.consent_required is False

    result = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)
    assert result["status"] == "no_change"
    assert result["put_count"] == 0
    assert client.puts == 0


def test_trackb_edit_outside_publishes_and_byte_preserves_subtree(tmp_path: Path) -> None:
    client = BodyClient()
    client.storage = _TRACK_B
    path = tmp_path / "page.md"
    pull_managed_suspending_the_write_policy(client, "123", path, no_assets=True)
    client.gets = 0
    # Append a new section AFTER the Track-B list — a source-splice-preservable edit.
    path.write_text(path.read_text(encoding="utf-8") + "\n## Added\n\nSafe paragraph.\n", encoding="utf-8")

    preflight = build_managed_preflight(client, "123", path)
    assert preflight.proof_mode == "exact_remote_prefix_append"
    assert preflight.consent_required is False
    # The published candidate keeps the original Track-B storage as an exact byte prefix.
    assert preflight.candidate_storage.startswith(_TRACK_B)

    result = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)
    assert result["status"] == "reconciled"
    assert client.puts == 1
    # The stored remote body still begins with the untouched Track-B subtree.
    assert client.storage.startswith(_TRACK_B)


def test_trackb_edit_inside_fails_closed_without_consent(tmp_path: Path) -> None:
    client = ReadOnlyClient(_TRACK_B)
    path = tmp_path / "page.md"
    pull_managed_suspending_the_write_policy(client, "123", path, no_assets=True)
    # Edit INSIDE the unrepresentable ordered-list item.
    path.write_text(path.read_text(encoding="utf-8").replace("2. b", "2. changed"), encoding="utf-8")

    with pytest.raises(ValidationError) as info:
        push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    # Hard fail-closed: an ownership-proof rejection, never a consent prompt, no PUT.
    assert not isinstance(info.value, MigrationConsentRequiredError)
    assert info.value.context is not None
    assert info.value.context["reason"] == "ownership_proof_invalid"
    assert client.puts == 0
