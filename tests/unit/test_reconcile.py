"""The four reconciliation commands, and the guarantees they are worth having for.

Ordered by what it costs when wrong. `record` replacing a canonical body against a
comparison that no longer holds is the worst thing here — it writes a document
reconciled with something that is gone, and it reads perfectly afterwards — so the
race tests come first.

Every test asserts the remote write count as well as the outcome. "The refusal
worked" is only evidence if nothing was written while it happened.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from atlassian_skills.confluence.pull_md import pull_md
from atlassian_skills.confluence.reconcile import (
    compare,
    compare_payload,
    rebaseline,
    record_reconciled_against,
    write_workspace,
)
from atlassian_skills.core.errors import ValidationError
from atlassian_skills.core.managed_manifest import (
    canonical_content_sha256,
    extract_asset_records,
    parse_managed_document,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.unit.conftest import HistoryClient  # noqa: E402

BASE = "<p>alpha paragraph text here</p><p>bravo paragraph text here</p><p>charlie text here</p>"


def _pulled(directory: Path, storage: str = BASE) -> tuple[HistoryClient, Path]:
    client = HistoryClient(storage=storage)
    client.history[client.version] = storage
    managed = directory / "page.md"
    pull_md(client, "123", output_path=managed, portable=True, no_assets=True)
    return client, managed


def _edit(path: Path, before: str, after: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert before in text, before
    path.write_text(text.replace(before, after), encoding="utf-8")


def _manifest_of(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    return parse_managed_document(
        text, assets=extract_asset_records(text), verify_content=False, verify_assets=False
    ).manifest


# --------------------------------------------------------------------------
# what it refuses to write
# --------------------------------------------------------------------------


def test_a_remote_that_moved_after_the_comparison_is_refused_by_name(tmp_path: Path) -> None:
    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    comparison = compare(client, "123", managed)
    fingerprint = comparison.fingerprint()

    # Somebody else saves while the agent is reading the diffs.
    client.move_to(BASE.replace("charlie text", "charlie edited"))
    reconciled = tmp_path / "reconciled.md"
    reconciled.write_text("alpha edited\n\nbravo\n\ncharlie edited\n", encoding="utf-8")
    before = managed.read_text(encoding="utf-8")

    with pytest.raises(ValidationError) as refused:
        record_reconciled_against(client, "123", managed, reconciled, compare_fingerprint=fingerprint)
    assert refused.value.context["reason"] == "remote_changed_since_compare"
    assert managed.read_text(encoding="utf-8") == before, "a refused record wrote to the file"
    assert client.puts == 0


def test_a_local_file_edited_after_the_comparison_is_refused_by_name(tmp_path: Path) -> None:
    """The other half, and it needs its own reason.

    Re-reading the diffs is the answer to a moved remote; comparing again against the
    file as it now is is the answer to a moved local. One reason for both would leave
    the caller to work out which.
    """

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    fingerprint = compare(client, "123", managed).fingerprint()

    _edit(managed, "bravo paragraph", "bravo also edited")
    reconciled = tmp_path / "reconciled.md"
    reconciled.write_text("whatever the agent produced\n", encoding="utf-8")

    with pytest.raises(ValidationError) as refused:
        record_reconciled_against(client, "123", managed, reconciled, compare_fingerprint=fingerprint)
    assert refused.value.context["reason"] == "local_changed_since_compare"
    assert client.puts == 0


def test_a_second_record_after_the_first_returned_is_refused(tmp_path: Path) -> None:
    """Two callers holding the same fingerprint, one after the other.

    This is the SEQUENTIAL case and it is worth having, but review R2 pointed out that
    it is not the concurrent one — it was named
    `test_two_processes_recording_the_same_file_leave_exactly_one_winner` and proved
    nothing about two processes. The real race has its own test below.
    """

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    first = compare(client, "123", managed).fingerprint()
    second = compare(client, "123", managed).fingerprint()
    assert first == second, "two comparisons of an unchanged pair must agree"

    one = tmp_path / "one.md"
    one.write_text("process one reconciled this\n", encoding="utf-8")
    two = tmp_path / "two.md"
    two.write_text("process two reconciled this\n", encoding="utf-8")

    receipt = record_reconciled_against(client, "123", managed, one, compare_fingerprint=first)
    assert receipt["status"] == "reconciled"

    with pytest.raises(ValidationError) as refused:
        record_reconciled_against(client, "123", managed, two, compare_fingerprint=second)
    assert refused.value.context["reason"] == "local_changed_since_compare"
    assert "process one" in managed.read_text(encoding="utf-8")
    assert "process two" not in managed.read_text(encoding="utf-8")
    assert client.puts == 0


def test_a_reconciled_file_that_is_itself_managed_is_refused(tmp_path: Path) -> None:
    """The agent is handed plain Markdown and returns plain Markdown. A managed
    document here would mean two manifests in the file that gets written."""

    client, managed = _pulled(tmp_path)
    fingerprint = compare(client, "123", managed).fingerprint()
    reconciled = tmp_path / "reconciled.md"
    reconciled.write_text(managed.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(ValidationError) as refused:
        record_reconciled_against(client, "123", managed, reconciled, compare_fingerprint=fingerprint)
    assert refused.value.context["reason"] == "reconciled_file_is_managed"


def test_an_empty_reconciled_file_is_refused(tmp_path: Path) -> None:
    """An empty body is almost certainly a lost edit, and publishing one deletes a
    page's contents. Cheap to refuse, expensive to allow."""

    client, managed = _pulled(tmp_path)
    fingerprint = compare(client, "123", managed).fingerprint()
    reconciled = tmp_path / "reconciled.md"
    reconciled.write_text("   \n\n", encoding="utf-8")

    with pytest.raises(ValidationError) as refused:
        record_reconciled_against(client, "123", managed, reconciled, compare_fingerprint=fingerprint)
    assert refused.value.context["reason"] == "reconciled_file_empty"


# --------------------------------------------------------------------------
# what it writes when it does write
# --------------------------------------------------------------------------


def test_a_record_binds_the_remote_projection_and_reports_both_body_hashes(tmp_path: Path) -> None:
    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    comparison = compare(client, "123", managed)
    reconciled = tmp_path / "reconciled.md"
    reconciled.write_text("alpha edited\n\nbravo paragraph text here\n\ncharlie text here\n", encoding="utf-8")

    receipt = record_reconciled_against(
        client, "123", managed, reconciled, compare_fingerprint=comparison.fingerprint()
    )

    manifest = _manifest_of(managed)
    assert manifest.base_md == canonical_content_sha256(comparison.remote), "base_md must be hash(R)"
    assert manifest.remote_version == comparison.remote_version
    assert manifest.v == 3
    assert receipt["body_sha256_before"] != receipt["body_sha256_after"]
    assert receipt["body_sha256_after"] == canonical_content_sha256(reconciled.read_text(encoding="utf-8"))
    assert receipt["remote_put_count"] == 0
    assert client.puts == 0


def test_a_record_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    """The replacement is a temp file, an fsync and a rename. A leftover `.atls.tmp`
    would be a half-written canonical document sitting next to the real one."""

    client, managed = _pulled(tmp_path)
    reconciled = tmp_path / "reconciled.md"
    reconciled.write_text("body\n", encoding="utf-8")
    record_reconciled_against(
        client, "123", managed, reconciled, compare_fingerprint=compare(client, "123", managed).fingerprint()
    )
    assert sorted(p.name for p in tmp_path.iterdir() if ".tmp" in p.name) == []


# --------------------------------------------------------------------------
# compare and prepare-reconcile write nothing canonical
# --------------------------------------------------------------------------


def test_compare_changes_neither_the_page_nor_the_file(tmp_path: Path) -> None:
    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    before = managed.read_text(encoding="utf-8")
    listing = sorted(p.name for p in tmp_path.iterdir())

    payload = compare_payload(compare(client, "123", managed))

    assert managed.read_text(encoding="utf-8") == before
    assert sorted(p.name for p in tmp_path.iterdir()) == listing, "compare created a file"
    assert client.puts == 0
    assert payload["compare_fingerprint"].startswith("remote:")


def test_compare_reports_a_dirty_local_file_as_dirty_and_not_as_invalid(tmp_path: Path) -> None:
    """§P2's fourth task. An author who edited their file has a `dirty` document, not
    a broken one, and the two demand opposite responses: carry on, or stop."""

    client, managed = _pulled(tmp_path)
    payload = compare_payload(compare(client, "123", managed))
    assert payload["local_dirty"] is False

    _edit(managed, "alpha paragraph", "alpha edited")
    payload = compare_payload(compare(client, "123", managed))
    assert payload["local_dirty"] is True
    assert payload["stale"] is False, "editing the local file does not make the page stale"


def test_compare_separates_stale_from_dirty(tmp_path: Path) -> None:
    client, managed = _pulled(tmp_path)
    client.move_to(BASE.replace("charlie text", "charlie edited"))
    payload = compare_payload(compare(client, "123", managed))
    assert payload["stale"] is True
    assert payload["local_dirty"] is False


def test_a_workspace_writes_only_inside_the_directory_it_was_given(tmp_path: Path) -> None:
    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    client.move_to(BASE.replace("charlie text", "charlie edited"))
    before = managed.read_text(encoding="utf-8")

    workspace = tmp_path / "work"
    report = write_workspace(compare(client, "123", managed), output_dir=workspace, managed_path=managed)

    assert managed.read_text(encoding="utf-8") == before, "the workspace touched the canonical file"
    assert sorted(p.name for p in workspace.iterdir()) == [
        "base.md",
        "local.md",
        "remote.md",
        "report.json",
    ]
    # §7.3: a suggested merge may exist and must not be presented as the answer. None
    # is written at all, because an artifact that looks like an answer becomes one.
    assert "suggested.md" not in {p.name for p in workspace.iterdir()}
    assert client.puts == 0
    assert report["files"]["base.md"].startswith(str(workspace))


def test_every_argv_the_workspace_hands_back_is_complete(tmp_path: Path) -> None:
    """§7.3: "report의 모든 argv는 placeholder 없는 실제 값".

    An argv with an angle bracket in it is a hole left for a human, and an agent will
    either run it verbatim or stop. Both are worse than being handed the real thing.
    """

    client, managed = _pulled(tmp_path)
    client.move_to(BASE.replace("charlie text", "charlie edited"))
    report = write_workspace(compare(client, "123", managed), output_dir=tmp_path / "work", managed_path=managed)
    for action in report["next_actions"]:
        assert not any("<" in part for part in action["argv"]), action["argv"]


# --------------------------------------------------------------------------
# rebaseline
# --------------------------------------------------------------------------


def test_rebaseline_moves_the_baseline_and_leaves_the_body_alone(tmp_path: Path) -> None:
    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    client.move_to(BASE.replace("charlie text", "charlie edited"))
    comparison = compare(client, "123", managed)
    body_before = managed.read_text(encoding="utf-8").partition("\n")[2]

    receipt = rebaseline(client, "123", managed, accept_remote_baseline=comparison.fingerprint())

    assert receipt["body_sha256_before"] == receipt["body_sha256_after"]
    assert managed.read_text(encoding="utf-8").partition("\n")[2] == body_before
    assert receipt["base_md_before"] != receipt["base_md_after"]
    assert _manifest_of(managed).base_md == canonical_content_sha256(comparison.remote)
    assert receipt["remote_put_count"] == 0
    assert client.puts == 0


def test_rebaseline_without_the_matching_approval_is_refused(tmp_path: Path) -> None:
    """The fingerprint is the approval. A rebaseline that accepted any token would be
    a way to move a baseline nobody looked at."""

    client, managed = _pulled(tmp_path)
    with pytest.raises(ValidationError) as refused:
        rebaseline(client, "123", managed, accept_remote_baseline="remote:sha256:0.local:sha256:0")
    assert refused.value.context["reason"] == "rebaseline_fingerprint_mismatch"
    assert client.puts == 0


def test_a_fingerprint_is_stable_for_an_unchanged_pair_and_moves_when_either_side_does(
    tmp_path: Path,
) -> None:
    """The property everything above rests on. If it were unstable, every record would
    be refused; if it ignored a side, a record would land against something else."""

    client, managed = _pulled(tmp_path)
    first = compare(client, "123", managed).fingerprint()
    assert compare(client, "123", managed).fingerprint() == first

    _edit(managed, "alpha paragraph", "alpha edited")
    after_local = compare(client, "123", managed).fingerprint()
    assert after_local != first

    client.move_to(BASE.replace("charlie text", "charlie edited"))
    after_remote = compare(client, "123", managed).fingerprint()
    assert after_remote != after_local


# --------------------------------------------------------------------------
# the race, actually raced
# --------------------------------------------------------------------------


def test_two_concurrent_records_leave_one_winner_and_a_named_refusal(tmp_path: Path) -> None:
    """§7.4 and AC7, with both callers inside the window at once.

    The test this replaces called `record` twice in sequence, so the first had already
    returned before the second began -- there was no window and nothing to arbitrate.
    Review R2 put a barrier where the two writers meet and got a bare
    `FileNotFoundError`: both passed the fingerprint check, both went on to write, and
    they shared one fixed temporary filename, so one renamed the file out from under
    the other.

    Threads rather than processes, deliberately. `flock` is held on the open file
    description, so two `os.open` calls exclude each other whether they are in one
    process or two -- and a thread test runs in the suite while a subprocess test needs
    a live server. The harness's `concurrent-record` lane covers real processes against
    a real page.

    What must come out is one success and one NAMED refusal. An unnamed filesystem
    exception is the failure mode, because a caller cannot tell it from a broken disk
    and will not know to compare again.
    """

    import threading

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    fingerprint = compare(client, "123", managed).fingerprint()

    bodies = {}
    for name in ("one", "two"):
        path = tmp_path / f"{name}.md"
        path.write_text(f"process {name} reconciled this\n", encoding="utf-8")
        bodies[name] = path

    outcomes: list[tuple[str, str]] = []
    lock = threading.Lock()
    start = threading.Barrier(2)

    def attempt(name: str) -> None:
        start.wait(timeout=10)
        try:
            receipt = record_reconciled_against(client, "123", managed, bodies[name], compare_fingerprint=fingerprint)
            with lock:
                outcomes.append(("ok", receipt["status"]))
        except ValidationError as refused:
            with lock:
                outcomes.append(("refused", refused.context["reason"]))

    threads = [threading.Thread(target=attempt, args=(name,)) for name in bodies]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(outcomes) == 2, f"a writer neither succeeded nor was refused: {outcomes}"
    winners = [value for kind, value in outcomes if kind == "ok"]
    losers = [value for kind, value in outcomes if kind == "refused"]
    assert winners == ["reconciled"], outcomes
    assert losers and losers[0] in {
        "record_in_progress",
        "local_changed_since_compare",
        "remote_changed_since_compare",
    }, f"the loser got an unnamed failure: {losers}"

    # Exactly one body landed, and it is one of the two that were offered.
    written = managed.read_text(encoding="utf-8")
    assert sum(f"process {name} reconciled this" in written for name in bodies) == 1
    assert client.puts == 0
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_the_temporary_file_name_is_unique_per_attempt(tmp_path: Path) -> None:
    """The second half of R2's finding, pinned separately.

    The lock is what makes two writers unable to meet. A unique temporary name is what
    keeps a bug in the lock from turning back into the bare `FileNotFoundError` that
    review found -- defence in depth, and cheap.
    """

    from atlassian_skills.confluence import reconcile as module

    seen = set()
    real = module.os.replace

    def capture(source: object, target: object) -> None:
        seen.add(str(source))
        real(source, target)

    client, managed = _pulled(tmp_path)
    module.os.replace = capture  # type: ignore[assignment]
    try:
        for index in range(3):
            body = tmp_path / f"body-{index}.md"
            body.write_text(f"revision {index}\n", encoding="utf-8")
            record_reconciled_against(
                client,
                "123",
                managed,
                body,
                compare_fingerprint=compare(client, "123", managed).fingerprint(),
            )
    finally:
        module.os.replace = real  # type: ignore[assignment]

    assert len(seen) == 3, f"the temporary name was reused: {seen}"
    assert all(".atls." in name and name.endswith(".tmp") for name in seen), seen


def test_no_record_path_leaves_an_artefact_in_the_canonical_directory(tmp_path: Path) -> None:
    """Success and every refusal, checked against the directory listing.

    Review R2's second round found the first version of the lock creating
    `<name>.atls.lock` and never removing it, so every record and every REFUSAL left a
    permanent file in a directory somebody keeps in Git. §7.4 says record replaces
    exactly one file and §10.1 says the inline manifest is the only required persistent
    metadata; a caller that was refused must not change the working tree at all.

    The lock is the canonical file itself now, so there is nothing to leave behind.
    This asserts the listing rather than the absence of one filename, because the next
    such artefact will have a different name.
    """

    client, managed = _pulled(tmp_path)
    body = tmp_path / "body.md"
    body.write_text("reconciled\n", encoding="utf-8")
    # No sidecar: AC1 stopped the pull writing one unless it is asked for.
    expected = {"page.md", "body.md"}
    assert {p.name for p in tmp_path.iterdir()} == expected, "the fixture itself changed"

    # A refusal on a fingerprint that never matched.
    with pytest.raises(ValidationError):
        record_reconciled_against(client, "123", managed, body, compare_fingerprint="remote:sha256:0.local:sha256:0")
    assert {p.name for p in tmp_path.iterdir()} == expected, "a refused record changed the tree"

    # A refusal on a reconciled file that is itself managed.
    managed_copy = tmp_path / "copy.md"
    managed_copy.write_text(managed.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ValidationError):
        record_reconciled_against(
            client,
            "123",
            managed,
            managed_copy,
            compare_fingerprint=compare(client, "123", managed).fingerprint(),
        )
    assert {p.name for p in tmp_path.iterdir()} == expected | {"copy.md"}

    # And a success.
    record_reconciled_against(
        client, "123", managed, body, compare_fingerprint=compare(client, "123", managed).fingerprint()
    )
    assert {p.name for p in tmp_path.iterdir()} == expected | {"copy.md"}


def test_rebaseline_leaves_no_artefact_either(tmp_path: Path) -> None:
    """Same contract, same lock, and it took the same fix."""

    client, managed = _pulled(tmp_path)
    before = {p.name for p in tmp_path.iterdir()}
    client.move_to(BASE.replace("charlie text", "charlie edited"))

    with pytest.raises(ValidationError):
        rebaseline(client, "123", managed, accept_remote_baseline="remote:sha256:0.local:sha256:0")
    assert {p.name for p in tmp_path.iterdir()} == before

    rebaseline(client, "123", managed, accept_remote_baseline=compare(client, "123", managed).fingerprint())
    assert {p.name for p in tmp_path.iterdir()} == before


def test_the_record_lock_coordinate_depends_on_the_document_and_nothing_else() -> None:
    """R2-AC7-1, checked where it can be checked from a Linux runner.

    The Windows branch byte-locked a sentinel under `tempfile.gettempdir()`, which reads
    `TMP`/`TEMP` from the process environment. Two processes with different `TMP` would lock
    two different sentinels and neither would see the other, so a caller's environment could
    move the arbitration domain off the document §7.4 protects. Review R2 found it; the
    branch had never executed, so nothing else could have.

    The Windows behaviour itself cannot be exercised here. What can be is the property the
    defect violated: the lock coordinate is derived from the resolved document path alone.
    So this reads the source and refuses the ingredients that make a coordinate movable.

    A source scan is a weak test and it is the right weak test: a stronger one would need a
    Windows runner, which is a separate release gate, and the alternative is nothing at all.
    """

    import ast
    import inspect
    import textwrap

    from atlassian_skills.confluence import reconcile as module

    raw = inspect.getsource(module._held.__wrapped__ if hasattr(module._held, "__wrapped__") else module._held)
    # The docstring is stripped before scanning, because it *explains* the defect and names
    # `gettempdir` while doing so. A guard that reads prose fires on the explanation and can
    # be silenced by rewording it -- neither of which has anything to do with the code.
    tree = ast.parse(textwrap.dedent(raw))
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    body = function.body[1:] if ast.get_docstring(function) else function.body
    source = "\n".join(ast.unparse(node) for node in body)
    movable = {
        "gettempdir": "reads TMP/TEMP from the environment",
        "getcwd": "depends on where the process was started",
        "expanduser": "depends on HOME",
        "environ": "reads the environment directly",
    }
    for token, why in movable.items():
        assert token not in source, f"the lock coordinate uses {token}, which {why}"
    assert "path.resolve()" in source, "the coordinate is no longer derived from the document path"
