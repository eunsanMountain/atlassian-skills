"""A stale push says whether the two edits can be combined.

Measured across 55 live pages: every managed push against a page someone else had
touched came back `remote_stale`, 55 out of 55, and stopped there. The refusal is
correct -- publishing over an unseen change is exactly what must not happen -- but
on its own it is a dead end, and a dead end is what sends people back to the
browser to redo the edit by hand.

Most of those are not conflicts. Someone fixed a typo three sections away.

So the refusal now carries the answer to the next question. It still refuses:
merging and publishing on the caller's behalf would do something they did not ask
for, to a page they have not seen since it moved.

Every outcome is named, including "cannot tell". A silent omission reads as "no
merge possible", and a caller would redo by hand an edit that could have been
combined in one step.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

from atlassian_skills.confluence.migration_preflight import build_managed_preflight
from atlassian_skills.confluence.pull_md import pull_md
from atlassian_skills.confluence.sidecar import sidecar_path
from atlassian_skills.core.errors import StaleError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.unit.conftest import HistoryClient  # noqa: E402

BASE = "<p>alpha paragraph text here</p><p>bravo paragraph text here</p><p>charlie text here</p>"


def _pulled(directory: Path, storage: str = BASE, *, base_cache: bool = False) -> tuple[HistoryClient, Path]:
    """Pull one managed file, optionally asking for the base cache as well.

    AC1/§10.1 stopped the pull writing that cache unless it is asked for, so a test
    whose subject *is* the cache -- a sidecar for another page, a missing one, a future
    schema -- has to ask. Tests about the merge itself do not, and now take the history
    path, which is the one §5.4 puts first and the one the field will use.
    """

    client = HistoryClient(storage=storage)
    managed = directory / "page.md"
    pull_md(client, "123", output_path=managed, portable=True, no_assets=True, write_base_cache=base_cache)
    return client, managed


def _stale_context(client: HistoryClient, managed: Path) -> dict:
    with pytest.raises(StaleError) as stale:
        build_managed_preflight(client, "123", managed)
    return stale.value.context


def test_a_remote_change_elsewhere_reports_a_merge_is_available() -> None:
    """The case worth reporting. Two people touched the page and neither touched
    the other's paragraph."""

    with tempfile.TemporaryDirectory() as directory:
        client, managed = _pulled(Path(directory), base_cache=True)
        managed.write_text(
            managed.read_text(encoding="utf-8").replace("alpha paragraph", "alpha edited"),
            encoding="utf-8",
        )
        client.storage = BASE.replace("charlie text", "charlie edited")
        client.version += 1

        context = _stale_context(client, managed)
        assert context["reason"] == "remote_stale"
        assert context["merge_available"] is True
        assert context["merge_conflicts"] == 0


def test_both_sides_editing_the_same_paragraph_reports_a_conflict() -> None:
    """Named as a conflict rather than reported as merge_available=False with no
    reason, so the caller knows the difference between "we cannot" and "you two
    disagree"."""

    with tempfile.TemporaryDirectory() as directory:
        client, managed = _pulled(Path(directory), base_cache=True)
        managed.write_text(
            managed.read_text(encoding="utf-8").replace("bravo paragraph", "bravo mine"),
            encoding="utf-8",
        )
        client.storage = BASE.replace("bravo paragraph", "bravo theirs")
        client.version += 1

        context = _stale_context(client, managed)
        assert context["merge_available"] is False
        assert context["merge_unavailable_reason"] == "conflict"
        assert context["merge_conflicts"] >= 1


def test_a_missing_base_snapshot_says_so_rather_than_saying_no() -> None:
    """Pulls made before sidecars existed have none. The push itself is
    unaffected; only the merge is unavailable, and reporting that as a flat "no"
    would send someone off to redo an edit that was mergeable."""

    with tempfile.TemporaryDirectory() as directory:
        client, managed = _pulled(Path(directory), base_cache=True)
        sidecar_path(managed).unlink()
        managed.write_text(
            managed.read_text(encoding="utf-8").replace("alpha paragraph", "alpha edited"),
            encoding="utf-8",
        )
        client.storage = BASE.replace("charlie text", "charlie edited")
        client.version += 1

        context = _stale_context(client, managed)
        assert context["merge_available"] is False
        assert context["merge_unavailable_reason"] == "sidecar_missing"


def test_a_sidecar_from_another_page_does_not_offer_a_merge() -> None:
    """Merging against another document's base produces something that reads fine
    and is wrong, which is worse than refusing."""

    with tempfile.TemporaryDirectory() as directory:
        client, managed = _pulled(Path(directory), base_cache=True)
        sidecar_path(managed).write_text(
            sidecar_path(managed).read_text(encoding="utf-8").replace('"123"', '"999"'),
            encoding="utf-8",
        )
        managed.write_text(
            managed.read_text(encoding="utf-8").replace("alpha paragraph", "alpha edited"),
            encoding="utf-8",
        )
        client.storage = BASE.replace("charlie text", "charlie edited")
        client.version += 1

        context = _stale_context(client, managed)
        assert context["merge_available"] is False
        assert context["merge_unavailable_reason"] == "sidecar_page_mismatch"


def test_the_outlook_never_publishes() -> None:
    """It answers a question. Merging and publishing on the caller's behalf would
    do something they did not ask for, to a page they have not seen since it
    moved."""

    with tempfile.TemporaryDirectory() as directory:
        client, managed = _pulled(Path(directory), base_cache=True)
        managed.write_text(
            managed.read_text(encoding="utf-8").replace("alpha paragraph", "alpha edited"),
            encoding="utf-8",
        )
        client.storage = BASE.replace("charlie text", "charlie edited")
        client.version += 1

        _stale_context(client, managed)
        assert client.puts == 0


def test_the_refusal_names_the_command_that_lays_the_merge_out() -> None:
    """Knowing the two edits combine and having no way to combine them is one
    step short of the browser. The argv carries the page and the file, so the
    caller does not compose it from parts of the error message."""

    with tempfile.TemporaryDirectory() as directory:
        client, managed = _pulled(Path(directory), base_cache=True)
        managed.write_text(
            managed.read_text(encoding="utf-8").replace("alpha paragraph", "alpha edited"),
            encoding="utf-8",
        )
        client.storage = BASE.replace("charlie text", "charlie edited")
        client.version += 1

        (action,) = _stale_context(client, managed)["next_actions"]
        assert action["argv"][:5] == ["confluence", "page", "md", "prepare-merge", "123"]
        assert str(managed) in action["argv"]


def test_a_conflict_also_gets_the_command() -> None:
    """The three files and the conflict locations are exactly what resolving one
    needs, and the line merger calls things conflicts that a reader settles in a
    moment -- a repeated table row, a moved block."""

    with tempfile.TemporaryDirectory() as directory:
        client, managed = _pulled(Path(directory), base_cache=True)
        managed.write_text(
            managed.read_text(encoding="utf-8").replace("bravo paragraph", "bravo mine"),
            encoding="utf-8",
        )
        client.storage = BASE.replace("bravo paragraph", "bravo theirs")
        client.version += 1

        context = _stale_context(client, managed)
        assert context["merge_available"] is False
        assert context["next_actions"][0]["argv"][2:4] == ["md", "prepare-merge"]


def test_a_missing_base_snapshot_offers_no_command_it_cannot_honour() -> None:
    """Naming prepare-merge here would hand the caller a command that refuses on
    contact, which reads as a broken tool rather than as a missing snapshot."""

    with tempfile.TemporaryDirectory() as directory:
        client, managed = _pulled(Path(directory), base_cache=True)
        sidecar_path(managed).unlink()
        client.storage = BASE.replace("charlie text", "charlie edited")
        client.version += 1

        assert "next_actions" not in _stale_context(client, managed)


def test_the_refusal_still_names_the_versions_it_disagreed_about() -> None:
    """The new detail is additional, not a replacement. Anything already reading
    this context keeps working."""

    with tempfile.TemporaryDirectory() as directory:
        client, managed = _pulled(Path(directory), base_cache=True)
        client.storage = BASE.replace("charlie text", "charlie edited")
        client.version += 1

        context = _stale_context(client, managed)
        assert "expected_version" in context
        assert "server_version" in context
        assert context["server_version"] != context["expected_version"]
