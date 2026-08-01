"""The failures the managed-reconciliation release exists to fix, pinned now.

Phase zero's job is to make each one reproducible before anything is implemented,
so later phases have something to turn green rather than a paragraph to interpret.
Every test here asserts the behaviour the SSOT specifies, not the behaviour the
code has today.

## Why `xfail(strict=True)` and not a list of failures in a document

A red test recorded once, in prose, decays. The command that produced it stops
being runnable, and by the time somebody implements the phase the description is
all that is left. These stay in the suite instead:

    the suite stays green      each one is a known, named, expected failure
    red stays reproducible     `pytest --runxfail <nodeid>` reports the real
                               failure with its real reason, any time
    green is enforced          `strict=True` means the day the behaviour lands,
                               the XPASS fails the suite and the marker has to
                               come off deliberately

`--runxfail` is the important half. Without it this file would be a list of
skipped tests nobody can see fail, which is how a fixture quietly stops matching
the defect it was written for.

## Each one was checked to fail for the reason it names

An xfail that fails on a typo -- a symbol that does not exist, a signature that
moved -- pins nothing while looking exactly like a pinned defect. The first draft
of this file had seven of those: `body_write.push_md` (the function lives in
`push_md.py`), `strip_managed_manifest` read as returning a string when it returns
a pair, `set_authority` called with a path where it takes a page id, and a
manifest rewriter looking for `key=value` on their own lines when the manifest is
one line. Every reason below was read out of a `--runxfail` run, not predicted.

## What each marker names

`PHASE` in the reason is the phase from SSOT §13 that closes it. Whoever
implements that phase removes the marker; if the assertion no longer describes
what the phase built, the assertion is what needs arguing about, and it is here.

Two fixtures are deliberately absent. `set-authority --to md` on the Jira side
and the unresolved added attachment reference were both closed in U0 and have real
green tests in `test_jira_description_merge.py` and `test_jira_description_push.py`.
A fixture asserting behaviour that already works would XPASS immediately.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from atlassian_skills.confluence.prepare_merge import finalize_merge, prepare_merge
from atlassian_skills.confluence.pull_md import pull_md
from atlassian_skills.confluence.push_md import push_md
from atlassian_skills.core.errors import ValidationError
from atlassian_skills.core.managed_manifest import (
    ManagedManifestError,
    canonical_content_sha256,
    parse_managed_document,
    strip_managed_manifest,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.unit.conftest import HistoryClient  # noqa: E402
from tests.unit.managed_seam import pull_managed_suspending_the_write_policy  # noqa: E402
from tests.unit.test_state_free_body_write import BodyClient  # noqa: E402

BASE = "<p>alpha paragraph text here</p><p>bravo paragraph text here</p><p>charlie text here</p>"

#: A table cell with a background colour. Measured: this grades `xhtml_required`
#: with `classification: unknown_blocked`, because the table topology and the
#: style attribute both come back unclassified.
UNCLASSIFIABLE = '<table><tbody><tr><td style="background-color: rgb(255,0,0);"><p>cell</p></td></tr></tbody></table>'


def _phase(number: str, what: str) -> pytest.MarkDecorator:
    return pytest.mark.xfail(strict=True, reason=f"PHASE {number}: {what}")


# --------------------------------------------------------------------------
# a client that remembers its own history
# --------------------------------------------------------------------------


def _pulled(
    directory: Path, client: BodyClient | None = None, storage: str = BASE, *, base_cache: bool = False
) -> tuple[Any, Path]:
    # A caller that supplies its own client still chooses the page's contents, and it
    # has to go through `set_storage` so the fake's history agrees with what the
    # manifest will bind. Plain assignment leaves history holding the default body.
    client = client if client is not None else HistoryClient()
    client.set_storage(storage)
    if isinstance(client, HistoryClient):
        client.history[client.version] = storage
    managed = directory / "page.md"
    pull_md(client, "123", output_path=managed, portable=True, no_assets=True, write_base_cache=base_cache)
    return client, managed


def _edit(path: Path, before: str, after: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert before in text, f"{before!r} is not in the document; the edit would be a no-op"
    path.write_text(text.replace(before, after), encoding="utf-8")


def _manifest_field(path: Path, field: str, value: str) -> None:
    """Rewrite one field of the manifest, which is a single line of `key=value`.

    A user can edit this file, which is exactly why §6.2 says schema validation is
    not enough on its own. Editing it here is how a test says "the manifest claims
    something the server does not agree with".

    Asserts the substitution happened. Silently rewriting nothing is how the first
    draft of this file produced four fixtures that failed inside their own helper.
    """

    text = path.read_text(encoding="utf-8")
    head, separator, rest = text.partition("\n")
    replaced = re.sub(rf"(?<= ){re.escape(field)}=\S+", f"{field}={value}", head, count=1)
    assert replaced != head, f"no {field}= pair in the manifest line of {path}"
    path.write_text(replaced + separator + rest, encoding="utf-8")


def _body_of(path: Path) -> str:
    return strip_managed_manifest(path.read_text(encoding="utf-8"))[0]


def _manifest_of(path: Path) -> Any:
    return parse_managed_document(
        path.read_text(encoding="utf-8"), assets=[], verify_content=False, verify_assets=False
    ).manifest


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 1-4. the historical base resolver (SSOT §5.4, AC2/AC3/AC4)
# --------------------------------------------------------------------------


def test_a_stale_file_with_no_sidecar_recovers_its_base_from_page_history(tmp_path: Path) -> None:
    """§5.4 step 1, and the reason AC1 can drop the sidecar at all.

    The base is already on the server: the manifest names the version the file was
    bound to, and Confluence still has that version's storage. Copying it into a
    local sidecar was never the only way to get it.

    Today this raises `sidecar_missing`, which is why
    `test_prepare_merge.py::test_no_base_snapshot_refuses_rather_than_merging_two_ways`
    passes. That test pins the CURRENT contract and this one pins the target, so P3
    moves both: the refusal survives only where history is genuinely unavailable.
    """

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    client.move_to(BASE.replace("charlie text", "charlie edited"))

    prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")

    assert client.history_calls, "the resolver never asked for the historical version"
    base = (tmp_path / "merge" / "base.md").read_text(encoding="utf-8")
    assert "alpha paragraph" in base, "the base is not the pre-edit projection"
    assert "charlie text here" in base, "the base carries the remote's later edit"


def test_history_whose_storage_does_not_match_the_manifest_is_refused(tmp_path: Path) -> None:
    """§5.4 step 1's second condition. A matching version number is not enough: a
    page can be restored or moved, and then version 7 is no longer the bytes the
    file was bound to. Accepting it merges against the wrong base, which reads
    perfectly and is wrong."""

    client, managed = _pulled(tmp_path)
    client.history[client.version] = "<p>something else entirely</p>"
    _edit(managed, "alpha paragraph", "alpha edited")
    client.move_to(BASE.replace("charlie text", "charlie edited"))

    with pytest.raises(ValidationError) as refused:
        prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")
    assert refused.value.context["reason"] == "historical_storage_hash_mismatch"


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("permission_denied", "history_permission_denied"),
        ("version_missing", "history_version_missing"),
    ],
)
def test_each_way_history_can_be_unavailable_gets_its_own_reason(tmp_path: Path, fault: str, reason: str) -> None:
    """§5.4: "history 조회 실패·권한 부족·버전 보존 만료 ... 서로 다른 reason code".

    Collapsing them loses the only thing the user can act on. A retention policy
    means reach for a cache; a permission refusal means ask an administrator.

    Asserted on the workspace rather than on an exception, and that correction is
    P3's. The fixture originally required a `ValidationError` — but §5.4 splits these
    two cases from the integrity failures precisely because they are AVAILABILITY
    failures: the local file is not in doubt, so step 4 hands back a two-way compare
    with the reason attached instead of stopping. Requiring a raise would have
    reintroduced the dead end the release exists to remove.

    The integrity failures do raise, and they have their own fixtures above.
    """

    client, managed = _pulled(tmp_path, HistoryClient(history_fault=fault))
    _edit(managed, "alpha paragraph", "alpha edited")
    client.move_to(BASE.replace("charlie text", "charlie edited"))

    payload = prepare_merge(client, "123", managed, output_dir=tmp_path / "merge").to_dict()

    assert payload["base_available"] is False
    assert payload["base_unavailable_reason"] == reason
    assert payload["candidate"] is None, "no base means no suggested merge"
    history = next(item for item in payload["base_attempts"] if item["source"] == "history")
    assert history["reason"] == reason


def test_a_base_projection_mismatch_is_not_downgraded_to_base_unavailable(tmp_path: Path) -> None:
    """§5.4's closing paragraph, and the subtle one.

    Everything checks out -- the version is retained, the storage hashes match, the
    converter and profile are the recorded ones -- and projecting that storage
    still does not reproduce `base_md`. That means the converter's output moved,
    not that the base is missing, so falling back to a two-way compare hides a
    converter change behind "could not find the base".

    §5.4 requires `manifest_base_projection_mismatch`, recoverable only through
    `rebaseline` with an approval fingerprint.
    """

    client, managed = _pulled(tmp_path)
    _manifest_field(managed, "base_md", "sha256:" + "0" * 64)
    _edit(managed, "alpha paragraph", "alpha edited")
    client.move_to(BASE.replace("charlie text", "charlie edited"))

    with pytest.raises(ValidationError) as refused:
        prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")
    assert refused.value.context["reason"] == "manifest_base_projection_mismatch"


# --------------------------------------------------------------------------
# 5. the baseline a reconciliation records (SSOT §5.1, AC6)
# --------------------------------------------------------------------------


def test_recording_a_reconciliation_binds_the_remote_projection_not_the_merged_body(
    tmp_path: Path,
) -> None:
    """The second of BASELINE.md's "two defects that shaped the plan", in the merge
    path.

    `base_md` answers "what was the last agreed common ancestor?". After a
    reconciliation that is the remote projection `R` the agent merged against --
    never the merged result `E`, which no remote version has ever held. Recording
    `E` makes the next comparison measure the local edit against itself, so a real
    remote change shows up as no change at all.

    Today `finalize_merge` writes `base_md=canonical_content_sha256(body)` with
    `body` the merged local body. That one line is what has to change.
    """

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    client.move_to(BASE.replace("charlie text", "charlie edited"))

    prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")
    remote_md = (tmp_path / "merge" / "remote.md").read_text(encoding="utf-8")
    reconciled = tmp_path / "merge" / "reconciled.md"
    reconciled.write_text(_body_of(managed).replace("charlie text", "charlie edited"), encoding="utf-8")

    result = finalize_merge(client, "123", managed, reconciled)
    rebound = _manifest_of(Path(result["path"]))

    assert rebound.base_md == canonical_content_sha256(remote_md), (
        "base_md is the hash of the merged local body, not of the remote projection the merge was made against"
    )


# --------------------------------------------------------------------------
# 6. readback (SSOT §9, AC10/AC11)
# --------------------------------------------------------------------------


def test_a_publish_the_server_normalised_binds_what_the_server_kept(tmp_path: Path) -> None:
    """§9 invariants 8 and 9 and AC11 -- and they already hold, so this is unmarked.

    Written as a red fixture first, on the assumption from BASELINE.md's second
    "defect that shaped the plan". Measured, both bindings were already right: with
    the server appending a newline on save, `remote_storage` is the hash of the
    stored bytes and `base_md` is the hash of their re-projection. That defect was
    fixed in the finalize-merge path and the ordinary publish path does not have it.

    Kept as a passing test rather than deleted. It is the invariant P6 must not
    break while it adds the reporting around it, and an invariant nothing checks is
    one a refactor is free to lose.

    Note what this case cannot show: this normalisation projects back to the same
    Markdown, so `hash(R2)` and `hash(submitted)` are equal here and the test
    cannot tell them apart. A normalisation where they diverge was not constructed
    in U1; §9.10's `published_normalized` reporting is pinned by the fixture below
    instead, which does not need one.
    """

    client, managed = _pulled(tmp_path)
    _edit(managed, "alpha paragraph", "alpha edited")
    inner = client.update_page

    def normalising(**kwargs: Any) -> dict[str, object]:
        result = inner(**kwargs)
        # Semantically equal, byte-different: the shape a server actually produces.
        client.storage = client.storage + "\n"
        client.history[client.version] = client.storage
        return result

    client.update_page = normalising  # type: ignore[method-assign]
    push_md(client, "123", _body_of(managed), managed_path=managed)

    import cfxmark

    reprojected = cfxmark.strip_header_notice(
        cfxmark.to_md_artifact(client.storage, options=cfxmark.ConversionOptions(profile="editable")).markdown
    )
    manifest = _manifest_of(managed)
    assert manifest.remote_storage == f"sha256:{_sha256(client.storage)}"
    assert manifest.base_md == canonical_content_sha256(reprojected)


def test_a_readback_holding_another_body_is_not_reported_as_merely_pending(tmp_path: Path) -> None:
    """§9 invariant 12, and the same class of error U0 closed in the documents.

    Measured today: with the server storing a different body, the publish returns
    `status: readback_pending`. The manifest correctly does not advance -- asserted
    below, because that half already holds -- but the word is wrong in the
    direction that matters. `pending` says "we could not see what is there".
    Here we looked, and what is there is somebody else's body. §9.12 gives that
    outcome its own names, `manual_recovery` or `readback_mismatch`.

    This is `body_put_failed` versus `body_put_not_observed` from U0, mirrored: a
    state name asserting something other than what the code actually knows. There
    it claimed knowledge it lacked; here it disclaims knowledge it has.
    """

    client, managed = _pulled(tmp_path)
    before = _manifest_of(managed)
    _edit(managed, "alpha paragraph", "alpha edited")
    client.fault = "wrong_storage"

    result = push_md(client, "123", _body_of(managed), managed_path=managed)

    after = _manifest_of(managed)
    assert after.remote_version == before.remote_version, "the manifest advanced over an unknown body"
    assert after.remote_storage == before.remote_storage
    assert result["status"] in {"manual_recovery", "readback_mismatch"}, (
        f"a body the server is holding and we have read reports {result['status']!r}"
    )


def test_every_receipt_status_the_plan_promises_exists_in_the_source() -> None:
    """§12.4: the downstream adapter consumes five receipts and must keep them distinct.

    Measured: `published_normalized` appears nowhere in `src/`. The adapter is
    specified to branch on it, so a document the server normalised is
    indistinguishable from one it stored verbatim -- and §9.10 requires the local
    rewrite that follows to be reported.

    Unmarked, because it is a source scan rather than a behaviour: it names the gap
    now so P6 cannot close its fixture above while leaving the vocabulary short.
    The reverse direction of U0's guard, which caught documents naming states the
    code does not have; this catches the plan doing it.
    """

    source = Path(__file__).resolve().parents[2] / "src"
    text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(source.rglob("*.py")))
    missing = [
        name
        for name in ("readback_pending", "manual_recovery", "published_normalized", "reconciled", "no_change")
        if name not in text
    ]
    # `published_normalized` was the one gap when this file was written; P6 closed it.
    # The list stays here rather than being deleted: it is the adapter's whole
    # vocabulary, and the next receipt to go missing should fail here too.
    assert missing == [], f"receipt names the plan promises and the source lacks: {missing}"


# --------------------------------------------------------------------------
# 7. what a lossy pull is allowed to leave on disk (SSOT §8.2, AC8)
# --------------------------------------------------------------------------


def test_a_pull_graded_xhtml_required_writes_no_canonical_file(tmp_path: Path) -> None:
    """§8.2's table: for `xhtml_required` the default canonical write is "아니오".

    A cell with a background colour: `xhtml_required`, and no preservation capability
    covers `td@style:background-color`, so §8.2.1's second axis lands on "no" and the
    pull writes nothing.

    This fixture was red for two different reasons before it was green. First the grade
    was computed correctly and not acted on at all. Then, briefly, the row was
    deliberately not implemented, because implementing it as written removed the Markdown
    workflow from 27 of 55 live pages -- which is what §8.2.1 was written to resolve.
    Neither history is visible in the assertion, so it is written here.

    This one uses a real unclassifiable page. The other three no-write grades are
    covered below by supplying the grade directly, for the reason given there.
    """

    client = HistoryClient()
    client.storage = UNCLASSIFIABLE
    client.history[client.version] = UNCLASSIFIABLE
    output = tmp_path / "unsupported.md"

    pulled = pull_md(client, "123", output_path=output, portable=True, no_assets=True)

    assert pulled.compatibility["status"] == "xhtml_required", "this fixture no longer grades as it did"
    assert not output.exists(), "a page graded xhtml_required left a canonical Markdown file behind"


#: §8.2's table, as a table. Every grade whose default canonical write is "아니오".
#: The parametrisation below no longer reads from this, because one of the three is
#: a recorded divergence rather than a pending implementation -- so the list is kept
#: only to assert that the plan still says what this file was written against.
#: Four now, not three. `markdown_identity_bound` joined them when the row was corrected to
#: match §8.2's condition: that grade may only be produced by a build with a registered,
#: contract-tested identity-carry capability, and no registry exists, so this build writes
#: nothing for it.
NO_WRITE_GRADES = (
    "markdown_identity_bound",
    "migration_required",
    "converter_fix_required",
    "xhtml_required",
)


def test_the_plan_still_forbids_the_writes_this_file_was_written_against() -> None:
    """If §8.2 is amended, the fixtures above stop describing anything.

    The `xhtml_required` row is under discussion. When it moves, this fails, and
    whoever moved it is pointed at the two fixtures that encode the old reading
    instead of leaving them to be discovered later as mysterious expected failures.
    """

    from atlassian_skills.confluence.compatibility import PLAN_8_2_CANONICAL_WRITE

    forbidden = tuple(grade for grade, permitted in PLAN_8_2_CANONICAL_WRITE.items() if not permitted)
    assert forbidden == NO_WRITE_GRADES


# All three pass, including `xhtml_required`, and that is the useful distinction:
# this fixture supplies the write policy along with the grade, so it measures the
# *enforcement*, which works for every grade. The blocker above is about what the
# table says for `xhtml_required`, not about whether a "no" is obeyed.
@pytest.mark.parametrize("grade", NO_WRITE_GRADES)
def test_no_grade_that_forbids_a_canonical_write_produces_one(tmp_path: Path, grade: str) -> None:
    """The policy, tested as a policy.

    Review R1 pointed out that covering `xhtml_required` alone lets a later change
    fix that one grade and keep writing canonical Markdown for the other two.

    **The reason this fixture gave for not using real pages was wrong.** It said
    `migration_required` and `converter_fix_required` need pages the live corpus has
    and a fixture does not. `tests/unit/test_pull_compatibility.py` already had a
    real, minimal page for all five grades -- a highlight-coloured table cell and a
    list mixing wrapped and bare items -- and the real-page version of this policy
    test now lives there, next to them. I did not look before concluding it was
    impossible.

    This one is kept anyway, and only for what it does differently: it supplies the
    grade instead of provoking it, so it holds even for a grade no page in the
    corpus currently produces. That is a real unit boundary -- §8.2 specifies a
    mapping from grade to write policy -- and it is a weaker test than the one next
    to the pages, not a substitute for it.
    """

    # `managed_pull`, not `pull_md`. The portable pull is the one §8.2 is about,
    # and it reads `compatibility_payload` through its own module global. Patching
    # `pull_md`'s name rebinds a different reference to the same function: measured,
    # the pull still reported `markdown_ready`, so this fixture spent its first life
    # asserting that an ungraded page writes a file. It failed for a reason that had
    # nothing to do with its subject, which is the same trap as a minimal fixture
    # passing while the bug lives -- read from the other end.
    import atlassian_skills.confluence.managed_pull as module

    real = module.compatibility_payload

    def graded(*args: Any, **kwargs: Any) -> dict[str, Any]:
        payload = real(*args, **kwargs)
        payload["status"] = grade
        payload["attention_required"] = True
        payload["attention_reason"] = grade
        # Spelled from §8.2's table rather than copied from the code being tested:
        # these three grades are exactly the ones whose canonical write is "no".
        payload["canonical_write_permitted"] = False
        return payload

    client = HistoryClient()
    output = tmp_path / f"{grade}.md"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, "compatibility_payload", graded)
        pulled = pull_md(client, "123", output_path=output, portable=True, no_assets=True)

    assert pulled.compatibility["status"] == grade, "the patch did not reach the code under test"
    assert not output.exists(), f"a page graded {grade} left a canonical Markdown file behind"


def test_a_recorded_profile_that_is_not_the_running_one_is_refused(tmp_path: Path) -> None:
    """§5.4 requires the converter **and the profile** to match the runner exactly.

    A separate test from the converter one, because review R2 pointed out that a
    single fixture changing only `converter` can be made green by a P3 change that
    compares converters and never compares profiles. Two independent failures are
    what make both comparisons load-bearing.

    Also distinct from the self-contradiction fixture: `editable` is a real cfxmark
    profile, so this document is internally consistent and simply recorded under a
    profile the runner is not using. Nothing here is malformed — it is a base that
    cannot be reproduced.
    """

    client, managed = _pulled(tmp_path)
    _manifest_field(managed, "profile", "editable")
    _edit(managed, "alpha paragraph", "alpha edited")
    client.move_to(BASE.replace("charlie text", "charlie edited"))

    with pytest.raises(ValidationError) as refused:
        prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")
    assert refused.value.context["reason"] == "historical_profile_mismatch"


def test_a_recorded_converter_that_is_not_the_running_one_is_refused(tmp_path: Path) -> None:
    """§5.4 step 1's third condition, and P0 asks for it by name.

    `M0` is only reproducible with the converter that produced it, and §16 forbids
    installing an old one to try. So a manifest recorded under a different
    converter cannot be projected into a trustworthy base, and the honest outcome
    is a named refusal rather than a base that looks right.

    Distinct from the base-projection fixture above: there the converter agrees and
    the projection still differs, which is a converter that changed under a fixed
    name. Here the name itself disagrees.
    """

    client, managed = _pulled(tmp_path)
    _manifest_field(managed, "converter", "cfxmark/0.0.1")
    _edit(managed, "alpha paragraph", "alpha edited")
    client.move_to(BASE.replace("charlie text", "charlie edited"))

    with pytest.raises(ValidationError) as refused:
        prepare_merge(client, "123", managed, output_dir=tmp_path / "merge")
    assert refused.value.context["reason"] == "historical_converter_mismatch"


# --------------------------------------------------------------------------
# 8-9. the manifest itself (SSOT §6.2, AC12)
# --------------------------------------------------------------------------


def test_a_manifest_from_a_newer_writer_is_refused_by_its_own_name(tmp_path: Path) -> None:
    """§6.2 and AC12 name `managed_manifest_newer_version` specifically.

    Today both directions collapse into `unsupported_managed_manifest_version`, so
    a document from a NEWER atls is indistinguishable from a corrupt or ancient
    one. They call for opposite actions -- upgrade, versus repair or re-pull -- and
    the message cannot say which while the reason cannot tell them apart.

    The refusal is already fail-closed and already does not echo the payload
    (§6.3's fourth bullet), which the companion test below pins as it stands.
    """

    _, managed = _pulled(tmp_path)
    _manifest_field(managed, "v", "4")

    with pytest.raises(ManagedManifestError) as refused:
        parse_managed_document(
            managed.read_text(encoding="utf-8"), assets=[], verify_content=False, verify_assets=False
        )
    assert getattr(refused.value, "reason", None) == "managed_manifest_newer_version"


def test_an_unreadable_manifest_version_never_echoes_the_document(tmp_path: Path) -> None:
    """§6.3's fourth bullet, and it holds today -- pinned so P2's rename cannot
    lose it. A refusal that quotes the body it could not parse puts page content
    into a log."""

    _, managed = _pulled(tmp_path)
    _manifest_field(managed, "v", "4")

    with pytest.raises(ManagedManifestError) as refused:
        parse_managed_document(
            managed.read_text(encoding="utf-8"), assets=[], verify_content=False, verify_assets=False
        )
    assert "alpha paragraph" not in str(refused.value)


def test_a_manifest_that_contradicts_itself_is_refused(tmp_path: Path) -> None:
    """§6.2's "동일 파일 안 서로 모순되는 authority/passthrough/profile 거부".

    Measured today: a managed Markdown document whose `profile` says
    `xhtml-exact` parses without complaint. The document then claims to be an
    exact-XHTML artefact while carrying Markdown that a Markdown push will convert,
    and whichever field the code happens to read decides what gets published.
    """

    _, managed = _pulled(tmp_path)
    _manifest_field(managed, "profile", "xhtml-exact")

    with pytest.raises(ManagedManifestError) as refused:
        parse_managed_document(
            managed.read_text(encoding="utf-8"), assets=[], verify_content=False, verify_assets=False
        )
    assert getattr(refused.value, "reason", None) == "managed_manifest_self_contradictory"


# --------------------------------------------------------------------------
# 10. authority may not be switched without a fresh grade (SSOT §10.2, AC15)
# --------------------------------------------------------------------------


def test_granting_markdown_authority_does_not_make_an_ungraded_body_publishable(
    tmp_path: Path,
) -> None:
    """§10.2, asserted as an outcome rather than as a signature.

    The first version of this fixture required `set_authority` to take a client, on
    the reasoning that it cannot re-grade what it cannot read. Review R1 was right
    to reject that: §10.2 explicitly permits DISABLING `--to markdown` instead, and
    a refusal needs no client — so the test would have blocked the compliant
    implementation while an unused `client=` parameter would have satisfied it.

    What §10.2 actually forbids is the effect: authority must not become a way to
    publish a body no grade approved. So this pulls a page Markdown cannot hold,
    confirms the Markdown copy cannot publish, hands authority to Markdown, and
    requires that it STILL cannot publish. Either outcome §10.2 allows passes —
    a refusal from `set_authority`, or a re-grade that keeps the push refused.
    """

    from atlassian_skills.confluence.xhtml_workflow import pull_xhtml, set_authority

    client = HistoryClient()
    client.storage = UNCLASSIFIABLE
    client.history[client.version] = UNCLASSIFIABLE

    # Both representations of the same page, each with its sidecar, which is the
    # state `set-authority` exists for. The Markdown copy is the one this page's
    # grade says must not publish.
    markdown = tmp_path / "page.md"
    # The seam: §8.2.1 stopped this page writing a Markdown file at all, which is a
    # stronger form of the same protection. The subject here is what `set-authority`
    # does when both copies exist, so the file has to exist for the question to be
    # asked -- and it is the question §10.2 answers, so it is worth still asking.
    pull_managed_suspending_the_write_policy(client, "123", markdown, no_assets=True)
    exact = tmp_path / "page.xhtml"
    pull_xhtml(client, "123", output_path=exact)
    body = _body_of(markdown).replace("cell", "cell edited by hand")

    def publishable() -> bool:
        try:
            push_md(client, "123", body, managed_path=markdown, dry_run=True)
        except Exception:
            return False
        return True

    assert not publishable(), "this fixture starts from a body that already cannot publish"

    # §10.2's own fallback: refusing is a compliant outcome, so a refusal here is
    # not the thing under test -- what happens to publishability afterwards is.
    with contextlib.suppress(ValidationError):
        set_authority("123", to="markdown", md_path=markdown, xhtml_path=exact)

    assert not publishable(), "switching authority to Markdown made an ungraded body publishable"


def test_the_two_products_spell_the_markdown_authority_differently() -> None:
    """A surface inconsistency, pinned where it will be read.

    `confluence page xhtml set-authority --to` accepts `markdown`; `jira issue
    description set-authority --to` accepts `md`. §7.1, §10.2 and §11.1 all write
    `--to md`. Two spellings for one concept is a thing an agent gets wrong once
    per session, and the SSOT already disagrees with one of them.

    Not marked xfail: nothing is broken, and choosing which spelling wins is a
    decision for P5/P7 rather than something to assert now. This fails the day
    somebody unifies them, which is when the plan text needs the same edit.
    """

    from atlassian_skills.confluence.xhtml_workflow import set_authority

    confluence_message = ""
    try:
        set_authority("123", to="md", md_path=Path("x.md"))
    except ValidationError as refused:
        confluence_message = str(refused)
    assert "markdown" in confluence_message and "'md'" not in confluence_message


# --------------------------------------------------------------------------
# 12. one owner for page identity (SSOT §12.1, AC17)
# --------------------------------------------------------------------------


def test_a_legacy_workflow_meta_block_before_the_manifest_is_already_refused(tmp_path: Path) -> None:
    """§12.1's competing binding, measured -- and it belongs to the downstream adapter.

    An earlier version of this file made it a P9 xfail on `parse_managed_document`,
    requiring a new reason (`managed_manifest_competing_binding`) for a
    `workflow:meta` block placed *after* the manifest. Review R1 rejected that,
    correctly:

    - §12.1 gives the job to the DOWNSTREAM ADAPTER migration -- read the legacy meta,
      adopt the managed file, remove the legacy block. It does not ask atls's
      general manifest parser to recognise an adapter-shaped HTML comment, and the SSOT
      never names that reason code.
    - A parser rule would also miss the failure that actually costs something: an
      adapter that reads the stale metadata FIRST and publishes to the page it
      names. No parser check prevents that.

    So the contract moves to where §12.1 puts it, recorded as a P9 item in U1.md.
    What is pinned here is what atls does today and must keep doing: the manifest
    has to be the first control block (§6.2), which already refuses the position the
    those documents actually use.
    """

    _, managed = _pulled(tmp_path)
    managed.write_text(
        "<!-- workflow:meta\nkey: PROJ-9\nconfluence_page_id: 999\nlast_synced_version: 3\n-->\n"
        + managed.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    with pytest.raises(ManagedManifestError) as refused:
        parse_managed_document(
            managed.read_text(encoding="utf-8"), assets=[], verify_content=False, verify_assets=False
        )
    assert getattr(refused.value, "reason", None) == "managed_manifest_not_first"


def test_a_workflow_meta_mentioned_in_prose_is_not_a_competing_binding(tmp_path: Path) -> None:
    """§6.2's last bullet and §12.1's last line: a document *about* the migration
    must not trip the checks the migration installs."""

    _, managed = _pulled(tmp_path)
    managed.write_text(
        managed.read_text(encoding="utf-8") + "\n\nThe adapter used to write a `workflow:meta` block carrying "
        "`confluence_page_id` and `last_synced_version`.\n",
        encoding="utf-8",
    )

    assert _manifest_of(managed).page == "123"


def test_the_jira_side_of_the_authority_switch_is_already_refused() -> None:
    """P0's fixture list names both products' unsafe `set-authority --to md`, and
    the Jira half was closed in U0 rather than left red.

    Kept here so the inventory covers what P0 asked for: review R1 noticed the Jira
    row was absent from this file entirely, and "closed in another module" is only
    checkable if something says so. This fails if the command is quietly re-enabled
    without the grading P7 owes it.
    """

    from atlassian_skills.jira.description_merge import set_authority as jira_set_authority

    with pytest.raises(ValidationError) as refused:
        jira_set_authority(HistoryClient(), "PROJ-1", Path("x.md"), to="md")
    assert refused.value.context["reason"] == "authority_to_md_unavailable"


# --------------------------------------------------------------------------
# the fixtures are inventoried, so one cannot quietly disappear
# --------------------------------------------------------------------------

#: Every expected failure in this file, and the phase that owns it. A phase closes
#: its rows by deleting the marker AND the row; the test below fails either way
#: round, so a marker cannot be dropped without the inventory noticing and the
#: inventory cannot claim a fixture that is gone.
#: Name -> why it is still red. Two kinds, and the difference is the point:
#:
#:   `P<n>`    nobody has written the code yet. It closes when that phase lands.
#:   `BLOCKED` the code is written and deliberately does something else, because
#:             doing what the fixture asks would need a decision this implementer
#:             does not get to take. It closes when the decision is taken, not when
#:             more code is written.
#:
#: A fixture stuck on `P5` forever is a schedule slip. A fixture stuck on `BLOCKED`
#: is a question nobody answered, and the two should not look alike in a list.
EXPECTED_FAILURES: dict[str, str] = {}


def test_the_inventory_matches_the_markers_in_this_file() -> None:
    """Read off the module rather than maintained beside it.

    A hand-kept list is the thing this checks against, so deriving one side from
    the source is what makes the check mean anything: a fixture whose marker was
    removed without finishing the work shows up as a mismatch here, and a fixture
    deleted outright shows up as a missing key.
    """

    module = sys.modules[__name__]
    marked: dict[str, str] = {}
    for name, function in vars(module).items():
        if not name.startswith("test_"):
            continue
        for mark in getattr(function, "pytestmark", []):
            if mark.name != "xfail":
                continue
            assert mark.kwargs.get("strict") is True, f"{name} is not a strict xfail"
            reason = str(mark.kwargs["reason"])
            label = reason.removeprefix("PHASE ").split(":", 1)[0].strip()
            # A blocked fixture states the blocker at length; the inventory carries
            # the one word, so the two do not have to be kept identical.
            marked[name] = "BLOCKED" if label.startswith("BLOCKED") else label

    assert marked == EXPECTED_FAILURES
    for name, label in marked.items():
        assert label == "BLOCKED" or re.fullmatch(r"P\d+", label), f"{name}: {label!r} is neither a phase nor a block"


def test_a_base_cache_naming_a_different_converter_is_not_used(tmp_path: Path) -> None:
    """§5.4 step 2 lists seven fields the cache must match, and two were unchecked.

    Review R2 reproduced it: a sidecar whose converter and profile were changed to
    names nothing produces was still adopted as a verified base, because
    `resolve_from_cache` compared only page, site, version, storage and base hash.

    It matters for the same reason the history path checks them. A base produced by a
    converter we are not running is not reproducible, so it is not a base -- however
    well its hashes line up. Reported and skipped, per §5.4: "하나라도 어긋나면 쓰지
    않고 그 사실을 reason으로 보고".
    """

    from atlassian_skills.confluence.base_resolver import resolve_from_cache
    from atlassian_skills.confluence.sidecar import read_sidecar, write_sidecar

    # `base_cache=True`: this fixture's subject is a cache that disagrees, so there has
    # to be one. AC1 stopped the pull writing it unless asked.
    client, managed = _pulled(tmp_path, base_cache=True)
    # Both take the DOCUMENT path and derive the sidecar's; handing them the sidecar
    # path appends the suffix twice.
    original = read_sidecar(managed, page_id="123")
    from dataclasses import replace as replace_fields

    write_sidecar(
        managed,
        replace_fields(original, converter="cfxmark/not-the-one-running", profile="not-the-profile"),
    )

    resolution = resolve_from_cache(managed, _manifest_of(managed), page_id="123")

    assert resolution.markdown is None, "a cache from another converter was adopted as a base"
    assert resolution.reason == "cache_disagrees_with_manifest"
    assert set(resolution.detail["fields"]) >= {"converter", "profile"}
