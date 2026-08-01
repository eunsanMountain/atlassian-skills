"""What an agent gets when a page moved under its edit.

The refusal already said *whether* the two edits combine. Knowing they combine
and having no way to combine them is one step short of the browser, so this lays
out the three versions as files and states what a publish of the result must
match.

The tests are ordered by what they cost when wrong. Merging against the wrong
base is worst -- it produces a document that reads perfectly well and is wrong --
so the refusals come first. Publishing from here is second: nothing in this path
may write to Confluence.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from atlassian_skills.confluence.prepare_merge import prepare_merge
from atlassian_skills.confluence.pull_md import pull_md
from atlassian_skills.confluence.sidecar import sidecar_path
from atlassian_skills.core.errors import ValidationError

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


def _edit(path: Path, before: str, after: str) -> None:
    path.write_text(path.read_text(encoding="utf-8").replace(before, after), encoding="utf-8")


def _moved(client: HistoryClient, before: str, after: str) -> None:
    client.storage = client.storage.replace(before, after)
    client.version += 1


# --------------------------------------------------------------------------
# What it refuses to guess
# --------------------------------------------------------------------------


def test_no_base_snapshot_falls_back_to_a_two_way_compare_and_says_so(tmp_path: Path) -> None:
    """Without a base, a merge cannot say which side changed what -- so it does not
    merge. What changed in P3 is what happens instead.

    This test used to require a refusal (`sidecar_missing`), and that refusal was the
    dead end the release exists to remove: measured across 55 live pages, every stale
    managed push ended there and sent someone back to the browser. §5.4 step 4 asks
    for the two files plus a named reason instead, with automatic merge and automatic
    record both closed off.

    So the guarantee is unchanged and the shape of it is not: no candidate is
    suggested, and the payload says `base_available: false` rather than leaving a
    caller to infer it from an empty conflict list.

    This client cannot serve history, which is why the resolver reaches step 4 at all.
    `history_unsupported` is the reason it reports, and it is a reason rather than a
    crash precisely so an older client object does not become an AttributeError.
    """

    # A client with no `get_page_history` at all, which is what "no base" now means:
    # the module's other tests use the history-capable fake because §5.4 puts history
    # first, and this is the one test whose subject is what happens when that step is
    # not available. `BodyClient` is that client, unmodified.
    from tests.unit.test_state_free_body_write import BodyClient

    client = BodyClient()
    client.storage = BASE
    managed = tmp_path / "page.md"
    pull_md(client, "123", output_path=managed, portable=True, no_assets=True)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")

    workspace = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")
    payload = workspace.to_dict()

    assert payload["base_available"] is False
    assert payload["base"] is None
    assert payload["candidate"] is None, "a two-way compare must not suggest a merge"
    assert payload["base_unavailable_reason"] == "history_unsupported"
    assert [item["source"] for item in payload["base_attempts"]] == ["history", "cache"]
    # The two it can offer are still written.
    assert (tmp_path / "merge" / "local.md").exists()
    assert (tmp_path / "merge" / "remote.md").exists()
    assert not (tmp_path / "merge" / "base.md").exists()


def test_a_base_snapshot_from_another_page_is_not_used(tmp_path: Path) -> None:
    """Managed files get copied between directories. Merging against another
    document's base produces something that reads fine and is wrong, which is the
    worst failure this code can have.

    Still true, and §5.4 step 2 says what to do about it: "하나라도 어긋나면 쓰지 않고
    그 사실을 reason으로 보고" -- do not use it, and report why. So the sidecar is
    rejected and named in the attempts rather than aborting the command, and the
    caller ends up in the same two-way compare as if it had never been there.
    """

    client, managed = _pulled(tmp_path, base_cache=True)
    # History has to be out of reach for the cache to be consulted at all: §5.4 tries
    # history first, and it now succeeds by default. Without this the base is found,
    # the cache is never read, and the rejection this test is about never happens --
    # the test would pass its way into measuring nothing.
    client.history_fault = "version_missing"
    side = sidecar_path(managed)
    side.write_text(side.read_text(encoding="utf-8").replace('"123"', '"999"'), encoding="utf-8")
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")

    payload = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge").to_dict()

    assert payload["base_available"] is False
    assert payload["candidate"] is None
    cache = next(item for item in payload["base_attempts"] if item["source"] == "cache")
    assert cache["reason"] == "cache_sidecar_page_mismatch"


def test_preparing_a_merge_never_publishes(tmp_path: Path) -> None:
    """It reads the page and writes local files. Publishing here would mean
    resolving, on the caller's behalf, a disagreement they have not seen."""

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")

    prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")
    assert client.puts == 0


# --------------------------------------------------------------------------
# The three files
# --------------------------------------------------------------------------


def test_the_three_versions_are_written_as_separate_files(tmp_path: Path) -> None:
    """The contract is three files, not a diff. An agent reads documents for
    meaning; base-to-local and base-to-remote are what that reading needs."""

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")

    workspace = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")
    base = workspace.base_path.read_text(encoding="utf-8")
    local = workspace.local_path.read_text(encoding="utf-8")
    remote = workspace.remote_path.read_text(encoding="utf-8")

    assert "alpha paragraph" in base and "charlie text" in base
    assert "alpha edited" in local and "charlie text" in local
    assert "alpha paragraph" in remote and "charlie edited" in remote


def test_the_manifest_line_is_not_a_change_both_sides_made(tmp_path: Path) -> None:
    """The header banner and manifest are written by the tool, not by an author,
    and only some of the three carry them. Left in, they read as an edit the
    remote made to every document and turn every merge into a conflict about a
    line nobody typed."""

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")

    workspace = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")
    for path in (workspace.base_path, workspace.local_path, workspace.remote_path):
        assert "cfxmark:manifest" not in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# The suggestion, and where it stops
# --------------------------------------------------------------------------


def test_two_edits_that_do_not_overlap_produce_a_candidate_holding_both(tmp_path: Path) -> None:
    """The common case measured live: someone fixed a typo three sections from
    the paragraph being edited."""

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")

    workspace = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")
    assert workspace.candidate_path is not None
    candidate = workspace.candidate_path.read_text(encoding="utf-8")
    assert "alpha edited" in candidate
    assert "charlie edited" in candidate
    assert workspace.conflicts == ()


def test_an_overlapping_edit_offers_no_candidate_and_says_where(tmp_path: Path) -> None:
    """A candidate here would have to pick a winner. The locations and both texts
    are reported instead, so the reader goes straight to the paragraph in
    question rather than diffing three files to find it."""

    client, managed = _pulled(tmp_path)
    _edit(managed, "bravo paragraph", "bravo mine")
    _moved(client, "bravo paragraph", "bravo theirs")

    workspace = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")
    assert workspace.candidate_path is None
    assert len(workspace.conflicts) >= 1
    conflict = workspace.conflicts[0]
    assert any("bravo mine" in line for line in conflict["local"])
    assert any("bravo theirs" in line for line in conflict["remote"])
    assert isinstance(conflict["base_start"], int)


# --------------------------------------------------------------------------
# What a publish of the result must match
# --------------------------------------------------------------------------


def test_the_version_reported_is_the_one_the_files_were_built_from(tmp_path: Path) -> None:
    """Publishing the merge against anything else republishes over a change
    nobody saw -- which is the failure the stale refusal exists to prevent, back
    again one step later."""

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")
    server_version = client.version

    workspace = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")
    assert workspace.remote_version == server_version


def test_the_reported_hash_is_of_the_body_this_run_read(tmp_path: Path) -> None:
    """Asserted against a hash computed here from the same storage rather than a
    constant, so a refactor that starts carrying the hash from the manifest --
    the stale value, by definition -- breaks here."""

    import hashlib

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")

    workspace = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")
    expected = hashlib.sha256(client.storage.encode("utf-8")).hexdigest()
    assert workspace.remote_storage_sha256 == expected


def test_the_next_action_names_the_files_the_rebind_needs(tmp_path: Path) -> None:
    """A caller that has to assemble the command from parts of the payload will
    assemble it wrong. Both paths are carried: the managed file supplies the
    manifest fields, the candidate supplies the body."""

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")

    payload = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge").to_dict()
    (action,) = payload["next_actions"]
    argv = action["argv"]

    assert argv[:5] == ["confluence", "page", "md", "finalize-merge", "123"]
    assert argv[argv.index("--md-file") + 1] == str(managed)
    assert argv[argv.index("--candidate") + 1] == payload["candidate"]


def test_the_command_writes_the_files_and_reports_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Asserted through the CLI because that is the layer that ships. A pull-time
    wiring gap once survived a green unit suite for exactly this reason: the test
    called the function one layer below the one people run.

    The default output directory is derived from the file rather than required,
    so the argv in a stale refusal is complete on its own."""

    import json

    from typer.testing import CliRunner

    from atlassian_skills.cli.main import app

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")
    monkeypatch.setattr("atlassian_skills.cli.confluence._make_client", lambda _ctx: client)

    result = CliRunner().invoke(
        app, ["confluence", "page", "prepare-merge", "123", "--md-file", str(managed), "--format", "json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema"] == "atls-merge-workspace-v1"
    assert Path(payload["base"]).parent == tmp_path / "page.md.merge"
    for key in ("base", "local", "remote", "candidate"):
        assert Path(payload[key]).read_text(encoding="utf-8")
    assert client.puts == 0


def test_a_conflicted_workspace_still_points_at_the_file_to_publish(tmp_path: Path) -> None:
    """There is no candidate to name, so the argv names the local file. Emitting
    a path that does not exist would hand the agent a command that fails on
    contact."""

    client, managed = _pulled(tmp_path)
    _edit(managed, "bravo paragraph", "bravo mine")
    _moved(client, "bravo paragraph", "bravo theirs")

    payload = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge").to_dict()
    assert payload["candidate"] is None
    for action in payload["next_actions"]:
        for flag in ("--md-file", "--candidate"):
            assert Path(action["argv"][action["argv"].index(flag) + 1]).exists()


# --------------------------------------------------------------------------
# Rebinding, which is what makes the merge publishable
# --------------------------------------------------------------------------


def test_the_workspace_candidate_cannot_be_published_directly(tmp_path: Path) -> None:
    """The gap this section exists to close, pinned so it cannot reopen.

    The workspace holds plain Markdown on purpose -- an agent has to read and
    edit it -- and `push-md` takes only a managed document. A first version
    pointed the next action straight at push-md, which ended the workflow one
    step short of the thing it exists for: the merged text was right there and
    nothing could publish it.
    """

    from atlassian_skills.confluence.push_md import push_md
    from atlassian_skills.core.errors import AtlasError

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")
    workspace = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")

    assert workspace.candidate_path is not None
    body = workspace.candidate_path.read_text(encoding="utf-8")
    assert "atls:managed" not in body

    with pytest.raises(AtlasError):
        push_md(client, "123", body, managed_path=workspace.candidate_path, dry_run=True)


def test_the_next_action_rebinds_rather_than_publishing(tmp_path: Path) -> None:
    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")

    (action,) = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge").to_dict()["next_actions"]
    assert action["argv"][2:4] == ["md", "finalize-merge"]
    assert action["requires_user_approval"] is False


def test_a_rebound_merge_publishes_and_carries_both_edits(tmp_path: Path) -> None:
    """The whole point: pull, edit, someone else edits, merge, publish -- without
    anyone retyping an edit or hand-writing a manifest."""

    from atlassian_skills.confluence.prepare_merge import finalize_merge
    from atlassian_skills.confluence.push_md import push_md

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")
    workspace = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")

    rebound = finalize_merge(client, "123", managed, workspace.candidate_path)
    merged = Path(rebound["path"])
    assert "alpha edited" in merged.read_text(encoding="utf-8")
    assert "charlie edited" in merged.read_text(encoding="utf-8")

    result = push_md(client, "123", merged.read_text(encoding="utf-8"), managed_path=merged, dry_run=True)
    assert result["status"] == "ready_to_publish"
    assert client.puts == 0


def test_a_page_that_moved_again_after_prepare_is_refused(tmp_path: Path) -> None:
    """The worst defect this workflow had, and it was pinned here as correct.

    Between preparing a merge and finishing one, an agent reads two diffs and may
    ask a person. If the page moves again in that time, the merge in hand was
    made against a version that no longer exists. Rebinding it to the current one
    made every later check pass -- the manifest was fresh, the version matched,
    the push was clean -- and destroyed the third party's edit in silence.

    It is not re-merged automatically either. The design rests on an agent having
    read both diffs, and it has not read this one.
    """

    from atlassian_skills.confluence.prepare_merge import finalize_merge

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")
    workspace = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")

    _moved(client, "bravo paragraph", "bravo moved again")
    with pytest.raises(ValidationError) as refused:
        finalize_merge(client, "123", managed, workspace.candidate_path)

    context = refused.value.context
    assert context["reason"] == "remote_changed_since_prepare"
    assert context["prepared_version"] == workspace.remote_version
    assert context["server_version"] == client.version
    assert context["next_actions"][0]["argv"][2:4] == ["md", "prepare-merge"]
    assert "bravo moved again" in client.storage
    assert client.puts == 0


def test_a_merge_prepared_against_the_current_page_still_rebinds(tmp_path: Path) -> None:
    """The refusal above must not swallow the ordinary case."""

    from atlassian_skills.confluence.prepare_merge import finalize_merge

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")
    workspace = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")

    rebound = finalize_merge(client, "123", managed, workspace.candidate_path)
    assert rebound["remote_version"] == workspace.remote_version


def test_a_candidate_with_no_recorded_basis_is_refused(tmp_path: Path) -> None:
    """An agent that assembled a candidate by hand, or moved it, leaves nothing
    to check the page against. Proceeding on an unverifiable merge is the same
    risk as the race above, arrived at by a different route."""

    from atlassian_skills.confluence.prepare_merge import BASIS, finalize_merge

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")
    workspace = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")
    (workspace.candidate_path.parent / BASIS).unlink()

    with pytest.raises(ValidationError) as refused:
        finalize_merge(client, "123", managed, workspace.candidate_path)
    assert refused.value.context["reason"] == "merge_basis_missing"
    assert client.puts == 0


def test_the_merged_file_sits_where_its_asset_references_resolve(tmp_path: Path) -> None:
    """Managed asset references resolve relative to the Markdown file holding
    them. A merged document written into `page.md.merge/` would look for
    `assets/x.png` inside the workspace directory and not find the author's
    file -- so the default sits beside the original."""

    from atlassian_skills.confluence.prepare_merge import finalize_merge

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")
    workspace = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")

    merged = Path(finalize_merge(client, "123", managed, workspace.candidate_path)["path"])
    assert merged.parent == managed.parent
    assert merged != managed


def test_prose_that_mentions_the_manifest_is_not_stripped(tmp_path: Path) -> None:
    """Dropping every line containing `atls:managed` deleted ordinary content: a
    document explaining this format, or a code sample printing the string, is
    prose. Only an exact leading manifest line is removed."""

    from atlassian_skills.confluence.prepare_merge import finalize_merge

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")
    workspace = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")
    assert workspace.candidate_path is not None
    workspace.candidate_path.write_text(
        workspace.candidate_path.read_text(encoding="utf-8")
        + '\nThis page explains the atls:managed header.\n\n    print("atls:managed")\n',
        encoding="utf-8",
    )

    merged = Path(finalize_merge(client, "123", managed, workspace.candidate_path)["path"])
    body = merged.read_text(encoding="utf-8")
    assert "explains the atls:managed header" in body
    assert 'print("atls:managed")' in body


def test_a_manifest_below_the_first_line_is_refused_rather_than_stripped(tmp_path: Path) -> None:
    """Not a document we understand. Guessing which half the author meant is how
    a merge quietly loses a section."""

    from atlassian_skills.confluence.prepare_merge import finalize_merge

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")
    workspace = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")
    assert workspace.candidate_path is not None
    header = managed.read_text(encoding="utf-8").splitlines()[0]
    workspace.candidate_path.write_text(
        workspace.candidate_path.read_text(encoding="utf-8") + f"\n{header}\n", encoding="utf-8"
    )

    with pytest.raises(ValidationError) as refused:
        finalize_merge(client, "123", managed, workspace.candidate_path)
    assert refused.value.context["reason"] == "candidate_manifest_misplaced"


def test_rebinding_leaves_the_canonical_file_alone(tmp_path: Path) -> None:
    """Nothing has been published yet. A canonical file already claiming the new
    remote version would be asserting something untrue about the server."""

    from atlassian_skills.confluence.prepare_merge import finalize_merge

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")
    workspace = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")
    before = managed.read_bytes()

    rebound = finalize_merge(client, "123", managed, workspace.candidate_path)
    assert managed.read_bytes() == before
    assert Path(rebound["path"]) != managed


def test_rebinding_refuses_a_file_belonging_to_another_page(tmp_path: Path) -> None:
    from atlassian_skills.confluence.prepare_merge import finalize_merge

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")
    workspace = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")

    with pytest.raises(ValidationError) as refused:
        finalize_merge(client, "999", managed, workspace.candidate_path)
    assert refused.value.context["reason"] == "managed_authority_mismatch"


def test_a_candidate_that_kept_a_manifest_line_does_not_get_two(tmp_path: Path) -> None:
    """The mistake an agent makes by copying the canonical file over the
    candidate. Cheap to tolerate, and a second manifest line parses as content."""

    from atlassian_skills.confluence.prepare_merge import finalize_merge

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")
    workspace = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")
    assert workspace.candidate_path is not None
    workspace.candidate_path.write_text(managed.read_text(encoding="utf-8"), encoding="utf-8")

    merged = Path(finalize_merge(client, "123", managed, workspace.candidate_path)["path"])
    assert merged.read_text(encoding="utf-8").count("atls:managed") == 1


def test_the_rebound_file_can_merge_again(tmp_path: Path) -> None:
    """A second round has to behave like the first, which means the sidecar base
    is the remote as it stands -- the same state a pull-then-edit produces, not
    the merged body."""

    from atlassian_skills.confluence.prepare_merge import finalize_merge

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    _moved(client, "charlie text", "charlie edited")
    workspace = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")
    merged = Path(finalize_merge(client, "123", managed, workspace.candidate_path)["path"])

    _moved(client, "bravo paragraph", "bravo theirs")
    second = prepare_merge(client, "123", merged, output_dir=tmp_path / "merge2")
    assert second.candidate_path is not None
    again = second.candidate_path.read_text(encoding="utf-8")
    assert "alpha edited" in again and "bravo theirs" in again
