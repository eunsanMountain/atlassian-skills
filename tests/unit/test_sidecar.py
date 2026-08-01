"""The base snapshot, and the ways it is allowed to be unusable.

A three-way merge needs the before-text of all three sides. The managed manifest
records `base_md` as a hash, and a hash can say that a file changed but never what
it said before -- so the base Markdown is written once, at pull time, beside the
file it is the base for.

The tests that matter most are the refusals. A sidecar that quietly belongs to
another page would merge one document's history into another and produce a
plausible result rather than an error, which is the worst kind of failure this
code can have.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

from atlassian_skills.confluence.pull_md import pull_md
from atlassian_skills.confluence.sidecar import (
    SCHEMA,
    Sidecar,
    SidecarUnusable,
    read_sidecar,
    sidecar_path,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.unit.test_state_free_body_write import BodyClient  # noqa: E402


def _pulled(directory: Path) -> Path:
    managed = directory / "page.md"
    pull_md(BodyClient(), "123", output_path=managed, portable=True, write_base_cache=True, no_assets=True)
    return managed


def test_a_pull_leaves_the_base_beside_the_document() -> None:
    with tempfile.TemporaryDirectory() as directory:
        managed = _pulled(Path(directory))
        assert sidecar_path(managed).name == "page.md.atls.json"
        whole = managed.read_text(encoding="utf-8")
        base = read_sidecar(managed, page_id="123").base_markdown
        # The body, not the whole file. A push compares against the body with the
        # manifest stripped, and a base that still carried the manifest would make
        # that line look like an edit both sides made -- a conflict on every merge.
        assert base in whole
        assert len(base) < len(whole)
        assert "atls:managed" not in base


def test_the_snapshot_is_the_text_and_not_a_hash_of_it() -> None:
    """The whole reason this file exists. `base_md` in the manifest is a hash, and
    a merge cannot reconstruct a document from one."""

    with tempfile.TemporaryDirectory() as directory:
        base = read_sidecar(_pulled(Path(directory)), page_id="123").base_markdown
        assert len(base) > 20
        assert "\n" in base


def test_the_snapshot_records_what_produced_it() -> None:
    """A base rendered by a different converter is not a base for this merge, and
    without these fields nobody could tell."""

    with tempfile.TemporaryDirectory() as directory:
        sidecar = read_sidecar(_pulled(Path(directory)), page_id="123")
        assert sidecar.converter.startswith("cfxmark ")
        assert sidecar.profile == "editable"
        assert sidecar.remote_version > 0
        assert sidecar.authority == "markdown"


# --------------------------------------------------------------------------
# The refusals
# --------------------------------------------------------------------------


def test_a_sidecar_for_another_page_is_refused() -> None:
    """Managed files get copied between directories. A base snapshot from the
    wrong page would merge one document's history into another and produce
    something that reads fine -- a failure with no error to notice."""

    with tempfile.TemporaryDirectory() as directory:
        managed = _pulled(Path(directory))
        with pytest.raises(SidecarUnusable) as unusable:
            read_sidecar(managed, page_id="999")
        assert unusable.value.reason == "sidecar_page_mismatch"


def test_a_missing_sidecar_is_named_rather_than_assumed() -> None:
    """Absent must be distinguishable from present-and-empty, because the caller
    reports "merge unavailable" and a silent fall back to two-way merge would
    overwrite the remote edit the merge existed to preserve."""

    with tempfile.TemporaryDirectory() as directory:
        managed = _pulled(Path(directory))
        sidecar_path(managed).unlink()
        with pytest.raises(SidecarUnusable) as unusable:
            read_sidecar(managed, page_id="123")
        assert unusable.value.reason == "sidecar_missing"


def test_a_corrupted_sidecar_is_refused_not_treated_as_absent() -> None:
    with tempfile.TemporaryDirectory() as directory:
        managed = _pulled(Path(directory))
        sidecar_path(managed).write_text("{ not json", encoding="utf-8")
        with pytest.raises(SidecarUnusable) as unusable:
            read_sidecar(managed, page_id="123")
        assert unusable.value.reason == "sidecar_unreadable"


def test_a_future_schema_is_refused() -> None:
    """A newer atls may store something this one would misread. Refusing is the
    only safe response to a format we do not know."""

    with tempfile.TemporaryDirectory() as directory:
        managed = _pulled(Path(directory))
        payload = json.loads(sidecar_path(managed).read_text(encoding="utf-8"))
        payload["schema"] = "atls-sidecar-v99"
        sidecar_path(managed).write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(SidecarUnusable) as unusable:
            read_sidecar(managed, page_id="123")
        assert unusable.value.reason == "sidecar_schema_mismatch"


def test_a_sidecar_without_the_base_text_is_incomplete() -> None:
    with tempfile.TemporaryDirectory() as directory:
        managed = _pulled(Path(directory))
        payload = json.loads(sidecar_path(managed).read_text(encoding="utf-8"))
        del payload["base_markdown"]
        sidecar_path(managed).write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(SidecarUnusable) as unusable:
            read_sidecar(managed, page_id="123")
        assert unusable.value.reason == "sidecar_incomplete"


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


def test_the_written_form_is_stable_and_readable() -> None:
    """Sorted keys and a trailing newline: this file sits in a repository next to
    the document, and a diff of it should show what changed rather than a
    reordering."""

    written = Sidecar(
        page_id="1",
        site="https://example.invalid",
        remote_version=3,
        remote_storage_sha256="sha256:" + "0" * 64,
        converter="cfxmark 0.5.1",
        profile="editable",
        base_markdown="# Title\n",
    ).to_json()
    assert written.endswith(b"\n")
    payload = json.loads(written)
    assert payload["schema"] == SCHEMA
    assert list(payload) == sorted(payload)


# --------------------------------------------------------------------------
# AC1 / §10.1: the base cache is not written unless it is asked for
# --------------------------------------------------------------------------


def test_a_pull_writes_no_base_cache_unless_it_is_asked_for(tmp_path: Path) -> None:
    """The v3 manifest is the only required persistent metadata.

    The sidecar carried a full second copy of the base Markdown, which made it a
    second source of truth that travels badly: it can be lost, copied away from its
    document, or left pointing at a file that has moved on. §5.4 recovers the base
    from the page history the manifest names, which the server still has.

    Keeping the default write was not neutral. Every downstream contract in this
    release -- historical recovery, manifest verification, the local-write ledger, the
    stale-compare refusals -- would have been passing because the sidecar was still
    there, and the measurement would have been of the sidecar rather than the workflow.
    """

    from tests.unit.conftest import HistoryClient

    client = HistoryClient()
    managed = tmp_path / "page.md"
    pull_md(client, "123", output_path=managed, portable=True, no_assets=True)

    assert managed.exists()
    # The whole directory, not the absence of one name: the next stray artefact will
    # have a different one.
    assert sorted(path.name for path in tmp_path.iterdir()) == ["page.md"]


def test_the_cache_is_written_when_it_is_asked_for(tmp_path: Path) -> None:
    """Offline and retention use are real, so the capability stays -- explicitly."""

    from tests.unit.conftest import HistoryClient

    client = HistoryClient()
    managed = tmp_path / "page.md"
    pull_md(client, "123", output_path=managed, portable=True, no_assets=True, write_base_cache=True)

    assert sorted(path.name for path in tmp_path.iterdir()) == ["page.md", "page.md.atls.json"]
    record = read_sidecar(managed, page_id="123")
    assert record.base_markdown, "a cache with no before-text is not a cache"


def test_a_cache_written_by_an_earlier_release_is_still_read(tmp_path: Path) -> None:
    """Backward compatibility is the reading side, not the writing side.

    A document pulled before this change has a sidecar beside it, and nothing should
    make that document worse to hold than a new one. Only the unrequested write is
    gone.
    """

    from atlassian_skills.confluence.base_resolver import resolve_from_cache
    from atlassian_skills.core.managed_manifest import strip_managed_manifest
    from tests.unit.conftest import HistoryClient

    client = HistoryClient()
    managed = tmp_path / "page.md"
    pull_md(client, "123", output_path=managed, portable=True, no_assets=True, write_base_cache=True)
    _body, manifest = strip_managed_manifest(managed.read_text(encoding="utf-8"))

    resolution = resolve_from_cache(managed, manifest, page_id="123")

    assert resolution.markdown is not None
    assert resolution.source == "cache"


def test_the_authority_a_managed_file_states_is_read_without_a_sidecar(tmp_path: Path) -> None:
    """The answer travels inside the file, which is why it is in the manifest.

    Reading only the sidecar answered `None` for a document whose first line says
    `authority=md` -- "nobody has decided" about a file that has. That mattered beyond
    tidiness: a storage pull beside such a file has to take authority off it, and a
    document invisible to that transfer goes on believing it may publish.
    """

    from atlassian_skills.confluence.sidecar import read_authority
    from tests.unit.conftest import HistoryClient

    client = HistoryClient()
    managed = tmp_path / "page.md"
    pull_md(client, "123", output_path=managed, portable=True, no_assets=True)

    assert not sidecar_path(managed).exists()
    assert read_authority(managed) == "markdown"
