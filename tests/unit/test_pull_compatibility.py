"""A pull says what kind of document you just received.

Before this, the answer arrived at push time as a refusal, and the refusal named
the ownership proof rather than the page: measured across 55 real pages, every
planted-loss scenario was blocked by a proof failure, not by any judgement about
content. 55 refusals, 0 explanations.

So the assessment moves to the pull. You learn what Markdown cannot hold for this
page *before* you edit it, and the same measurement is repeated against fresh
remote state at push time -- a pull-time answer is a forecast, and a forecast
nobody re-checks is how a stale approval gets reused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atlassian_skills.confluence.models import Page
from atlassian_skills.confluence.pull_md import pull_md

MACRO = (
    '<ac:structured-macro ac:name="info" ac:schema-version="1"{extra}>'
    "<ac:rich-text-body><p>note body</p></ac:rich-text-body>"
    "</ac:structured-macro>"
)

# One page per classification, each the smallest thing that produces it.
PAGES = {
    "markdown_ready": "<p>alpha paragraph text here</p><p>beta paragraph text here</p>",
    "markdown_identity_bound": MACRO.format(extra=' ac:macro-id="7f3a-0001"'),
    "migration_required": (
        "<table><thead><tr><th>h</th></tr></thead>"
        '<tbody><tr><td data-highlight-colour="#ff0000">a</td></tr></tbody></table>'
    ),
    "xhtml_required": (
        '<table><thead><tr><th>h</th></tr></thead><tbody><tr><td colspan="2">a</td></tr></tbody></table>'
    ),
}


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


def _pull(storage: str, tmp_path: Path) -> dict:
    """Through `pull_md`, not through `prepare_portable_pull`.

    An earlier version of this helper called the inner function directly. It
    computed the assessment correctly and `pull_md` never passed it on, so every
    test here passed while the command emitted `"compatibility": {}`. Only a live
    run found it. Testing one layer below the one that ships tests a layer that
    does not ship.
    """

    managed = tmp_path / "page.md"
    result = pull_md(FakeClient(storage), "1", output_path=managed, portable=True, no_assets=True)
    return result.compatibility


@pytest.mark.parametrize("expected", list(PAGES), ids=list(PAGES))
def test_a_pull_reports_the_pages_classification(expected: str, tmp_path: Path) -> None:
    payload = _pull(PAGES[expected], tmp_path)
    assert payload["schema"] == "atls-compatibility-v1"
    assert payload["status"] == expected


@pytest.mark.parametrize("storage", list(PAGES.values()), ids=list(PAGES))
def test_every_pull_carries_what_a_caller_needs_to_decide(storage: str, tmp_path: Path) -> None:
    """Each field here is something a caller would otherwise have to guess at, and
    guessing is what left agents inventing commands."""

    payload = _pull(storage, tmp_path)
    for key in (
        "status",
        "summary",
        "findings",
        "candidate_sha256",
        "workflow_decision_required",
        "requires_identity_carry",
        "recommended_workflow",
        "next_actions",
    ):
        assert key in payload, key
    assert payload["next_actions"], "a status with no way forward is a dead end"


def test_the_assessment_names_the_converter_and_profile_it_used(tmp_path: Path) -> None:
    """The same page is lossless under one conversion profile and blocked under
    another. An assessment that does not say which one it used cannot be
    re-checked, and a claim nobody can re-check is a guess."""

    payload = _pull(PAGES["markdown_ready"], tmp_path)
    assert payload["profile"] == "editable"
    assert payload["converter"].startswith("cfxmark ")


def test_a_macro_page_says_the_identity_must_be_carried(tmp_path: Path) -> None:
    """Measured: the server keeps a macro's id only while the body is
    byte-identical, and re-assigns it on any edit. Publishing such a page without
    carrying the id forward detaches every macro's comments and attachments, and
    nothing visible changes to warn anyone."""

    payload = _pull(PAGES["markdown_identity_bound"], tmp_path)
    assert payload["requires_identity_carry"] is True
    assert payload["workflow_decision_required"] is False


def test_findings_name_what_is_lost_and_how_much(tmp_path: Path) -> None:
    payload = _pull(PAGES["migration_required"], tmp_path)
    codes = {(finding["code"], finding["count"]) for finding in payload["findings"]}
    assert ("td@data-highlight-colour", 1) in codes


@pytest.mark.parametrize("status", list(PAGES), ids=list(PAGES))
def test_a_blocked_classification_is_still_a_successful_command(status: str, tmp_path: Path) -> None:
    """The property an agent depends on.

    `xhtml_required` means "Markdown is not a trustworthy round trip for this
    page" -- a fact about the page, not a broken command. An agent that sees a
    non-zero exit retries or gives up, and either way it never reads the
    findings that would have told it what to do instead."""

    from types import SimpleNamespace

    from typer.testing import CliRunner

    from atlassian_skills.cli.main import app

    payload = _pull(PAGES[status], tmp_path)
    runner = CliRunner()
    monkey = pytest.MonkeyPatch()
    monkey.setattr("atlassian_skills.cli.confluence._make_client", lambda _: object())
    monkey.setattr(
        "atlassian_skills.confluence.pull_md.pull_md",
        lambda *_a, **_k: SimpleNamespace(
            status="pulled",
            markdown="# x\n",
            version=1,
            title="t",
            assets=(),
            edit_guidance=(),
            warnings=(),
            losses=(),
            blockers=(),
            push_safe=True,
            migration_report=None,
            migration_report_sha256=None,
            compatibility=payload,
        ),
    )
    try:
        result = runner.invoke(
            app,
            ["confluence", "page", "pull-md", "1", "--output", str(tmp_path / "o.md"), "--format", "json"],
        )
    finally:
        monkey.undo()
    assert result.exit_code == 0, result.output
    import json

    assert json.loads(result.stdout)["compatibility"]["status"] == status


def test_a_page_that_loses_nothing_reports_no_findings(tmp_path: Path) -> None:
    """Differences we have already explained -- a macro's server-supplied
    schema-version, for one -- are not findings. Listing them would bury the ones
    that need a decision."""

    assert _pull(PAGES["markdown_ready"], tmp_path)["findings"] == []


# --------------------------------------------------------------------------
# §8.2's second column: which grades may leave a file, and what unlocks the rest
# --------------------------------------------------------------------------

#: What this build does, transcribed. Not derived from the code it checks -- a test that
#: reads the policy out of the mapping it is testing agrees with any change.
#:
#: `xhtml_required` is the default answer, not the whole answer: §8.2.1 gives that grade a
#: second axis, and a page whose unclassifiable structure has a closed preservation
#: capability is written after all. The per-page outcome is measured below.
#: What each grade permits **on its own**, with nothing registered. §8.2's row, and the
#: floor that a capability lifts rather than replaces.
CANONICAL_WRITE_FLOOR_BY_GRADE = {
    "markdown_ready": True,
    "markdown_identity_bound": False,
    "migration_required": False,
    "converter_fix_required": False,
    "xhtml_required": False,
}

#: What this build actually does, which is the floor plus whatever is registered. The two
#: are separate dicts because collapsing them is how the row ends up edited to `True`.
CANONICAL_WRITE_BY_GRADE = {
    "markdown_ready": True,
    # True as of A6, and what changed is the registry rather than this line. §8.2 makes a
    # statically registered identity-preservation capability the precondition for the
    # grade; `IDENTITY_PRESERVATION` is now that registration, bound to the converter and
    # profile it was proven against and closed by a contract test on the public push path.
    #
    # It was `False` for a cycle and the reason is worth keeping: the row said `True` with a
    # comment deferring the gate, so 14 of 55 live pages were written on a belief the gate
    # was supposed to check. The lesson is not "be conservative" but "the thing that flips
    # the row is the mechanism, never a comment about it" -- which is why the assertion
    # below reads the registry rather than trusting this dict.
    "markdown_identity_bound": True,
    "migration_required": False,
    "converter_fix_required": False,
    "xhtml_required": False,
}


def _pull_result(storage: str, tmp_path: Path, **kwargs: object):
    managed = tmp_path / "page.md"
    result = pull_md(FakeClient(storage), "1", output_path=managed, portable=True, no_assets=True, **kwargs)
    return result, managed


@pytest.mark.parametrize("grade", list(PAGES), ids=list(PAGES))
def test_only_the_grades_that_may_publish_leave_a_file(grade: str, tmp_path: Path) -> None:
    """The grade was computed correctly and then not acted on: every page got a file.

    Three of these five cannot be published from the file the pull used to write.
    That file looks like a work product, is not one, and is what the next person
    edits -- so for those three the pull now writes nothing at all.

    Every grade here comes from a real page rather than a supplied value, which is
    what makes this a test of the policy *and* of the grading that feeds it.
    """

    result, managed = _pull_result(PAGES[grade], tmp_path)

    permitted = CANONICAL_WRITE_BY_GRADE[grade]
    assert result.compatibility["status"] == grade, "this page no longer grades as it did"
    assert result.compatibility["canonical_write_permitted"] is permitted
    assert managed.exists() is permitted, f"{grade}: file exists={managed.exists()}, policy says {permitted}"
    assert (result.status == "not_pulled") is not permitted


def test_the_identity_grade_writes_because_of_the_registry_not_because_of_this_file() -> None:
    """The transcription above is a claim; this is what binds it to a mechanism.

    A hand-listed matrix and the code can drift apart in either direction, and the
    dangerous direction is the matrix saying `True` while nothing backs it -- which is the
    exact shape of the defect that made this grade `False` in the first place. So the
    permission is checked against the registry, and removing the registration must turn
    this red.
    """

    from atlassian_skills.confluence import preservation as module

    assert CANONICAL_WRITE_BY_GRADE["markdown_identity_bound"] is (module.IDENTITY_PRESERVATION is not None)


def test_this_build_and_the_plan_now_agree_everywhere() -> None:
    """The divergence is gone, and this is what keeps it gone.

    For one cycle §8.2's `xhtml_required` row said "no canonical write" and this build
    wrote the file, because implementing the row as written removed the Markdown workflow
    from 27 of the 55 pages in the live corpus. §8.2.1 resolved it by splitting the grade
    into two axes rather than by loosening either one, so the row is now implemented
    exactly and the second axis decides the write per page.

    Kept as an equality rather than deleted: the next disagreement should fail here.
    """

    from atlassian_skills.confluence.compatibility import (
        PLAN_8_2_CANONICAL_WRITE,
        STATUS_BY_CLASSIFICATION,
    )

    actual = {row.status: row.canonical_write_permitted for row in STATUS_BY_CLASSIFICATION.values()}
    assert actual == PLAN_8_2_CANONICAL_WRITE


def test_a_refused_pull_leaves_the_directory_exactly_as_it_found_it(tmp_path: Path) -> None:
    """Not just the canonical file: the sidecar and the assets too.

    Asserted as a full directory listing rather than as the absence of one name,
    for the reason R2 gave about the lock file -- the next stray artefact will have
    a different name, and a test that names the old one will not see it.
    """

    before = sorted(path.name for path in tmp_path.iterdir())
    result, _ = _pull_result(PAGES["migration_required"], tmp_path)

    assert result.status == "not_pulled"
    assert sorted(path.name for path in tmp_path.iterdir()) == before


def test_a_refused_pull_never_hands_back_a_command_against_the_file_it_did_not_write(tmp_path: Path) -> None:
    """The dead end this whole payload exists to remove, reintroduced by a refusal.

    Every no-write grade still recommends the Markdown or storage workflow, and the
    obvious next action is `md push --md-file <output>`. That file does not exist.
    An argv naming it is worse than a placeholder: a placeholder cannot be run, and
    this one runs and fails on something that reads like the caller's mistake.

    The direction is what matters, which is the same distinction that made
    `--body-file` wrong to add to the harness's local-write flags. An unwritten path
    after `--output` is where the file *would* go and is exactly right -- the
    approval re-pull has to name it. After `--md-file` it is a file being read, and
    that is the dead end.
    """

    consumes = {"--md-file", "-f", "--xhtml-file", "--body-file", "--file", "--candidate"}
    for grade in ("migration_required",):
        result, managed = _pull_result(PAGES[grade], tmp_path)
        assert result.status == "not_pulled", grade
        argvs = [action.get("argv", []) for action in result.compatibility["next_actions"]]
        argvs += [step.get("argv", []) for step in result.edit_guidance]
        for argv in argvs:
            for flag, value in zip(argv, argv[1:], strict=False):
                assert not (flag in consumes and value == str(managed)), (
                    f"{grade}: {flag} reads the file the pull did not write: {argv}"
                )
            for token in argv:
                assert "<" not in token and ">" not in token, f"{grade}: placeholder argv {argv}"
        assert any(argv for argv in argvs), f"{grade}: refused with no way forward"


def test_an_approval_matching_this_pulls_report_writes_the_file(tmp_path: Path) -> None:
    """§8.2: named losses, approved once by fingerprint, then pull again."""

    refused, managed = _pull_result(PAGES["migration_required"], tmp_path)
    assert refused.status == "not_pulled"
    assert not managed.exists()
    fingerprint = refused.migration_report_sha256
    assert fingerprint, "a refusal that cannot be approved is a dead end"

    accepted, managed = _pull_result(PAGES["migration_required"], tmp_path, accept_migration=fingerprint)

    assert accepted.status in {"pulled", "pulled_with_migrations"}
    assert managed.exists(), "an approved migration still wrote nothing"


def test_the_argv_the_refusal_hands_back_is_the_one_that_works(tmp_path: Path) -> None:
    """Not a paraphrase of it. The skill's rule is to run what is returned."""

    refused, managed = _pull_result(PAGES["migration_required"], tmp_path)
    approvals = [step for step in refused.edit_guidance if step.get("kind") == "approve_named_losses"]
    assert len(approvals) == 1, refused.edit_guidance
    argv = approvals[0]["argv"]

    assert argv[-2] == "--accept-migration"
    accepted, managed = _pull_result(PAGES["migration_required"], tmp_path, accept_migration=argv[-1])
    assert managed.exists(), "the fingerprint the refusal printed did not unlock the write"


def test_an_approval_that_does_not_match_the_report_is_refused(tmp_path: Path) -> None:
    """An approval carried over from a page that has since changed approves nothing.

    The whole point of a fingerprint is that it goes stale. Accepting any non-empty
    string would make this flag the `--force` §8.2 declines to add.
    """

    result, managed = _pull_result(PAGES["migration_required"], tmp_path, accept_migration="sha256:" + "0" * 64)

    assert result.status == "not_pulled"
    assert not managed.exists()


@pytest.mark.parametrize("grade", ["xhtml_required"])
def test_the_approval_flag_does_not_unlock_a_grade_that_has_no_approval_route(grade: str, tmp_path: Path) -> None:
    """The anti-`--force` test, and the reason the flag is grade-scoped.

    `xhtml_required` cannot be consented to at all, because an
    approval has to say what is being given up and this grade means we could not
    name it. So even a fingerprint-shaped value still writes nothing: the flag is
    not a general override that happens to be spelled long.
    """

    probe, _ = _pull_result(PAGES[grade], tmp_path)
    fingerprint = probe.migration_report_sha256 or "sha256:" + "0" * 64

    result, managed = _pull_result(PAGES[grade], tmp_path, accept_migration=fingerprint)

    assert result.status == "not_pulled", f"{grade} accepted an approval it has no route for"
    assert not managed.exists()


def test_the_write_policy_is_read_from_one_table_and_not_re_derived() -> None:
    """§8.2 is a mapping, and the code that enforces it must be the code that has it.

    A second copy anywhere -- an `if status in (...)` in the publisher, a set in the
    CLI -- is how the payload and the behaviour end up disagreeing about the same
    page. This pins the field onto the shared mapping rather than the enforcement
    site, so moving the policy out of the table breaks this test.

    **The floor, not the effective answer.** §8.2.1 has two axes: the row is what the
    grade permits on its own, and a registered capability can lift it. Comparing the row
    against the effective answer would force one of them to be wrong the moment anything
    is registered, and the temptation then is to edit the row -- which is the row saying
    `True` on its own, the thing §8.2 forbids and the shape of the original defect.
    """

    from atlassian_skills.confluence.compatibility import STATUS_BY_CLASSIFICATION

    by_status = {row.status: row.canonical_write_permitted for row in STATUS_BY_CLASSIFICATION.values()}
    assert by_status == CANONICAL_WRITE_FLOOR_BY_GRADE


# --------------------------------------------------------------------------
# §8.2.1: the second axis, and the contract that closes a capability
# --------------------------------------------------------------------------

#: One page per shape, and the answer §8.2.1 requires *of the discriminator*. Transcribed
#: from the decision, not read out of the registry -- a table derived from the code it
#: checks agrees with any change to that code.
#:
#: The registry is currently empty, because `table-splice-v1`'s contract test refused to
#: close it: the managed path rejects an edit to the merged-cell page with
#: `ownership_proof_invalid`, while the non-managed path publishes it with the colspan
#: intact. So the *behavioural* answer for every page here is "no capability, no write",
#: and the second column below is what the discriminator would say if the capability were
#: registered. Both are tested, separately, because conflating them is how an empty
#: registry would make the discriminator look correct without it ever running.
PRESERVATION_CASES = {
    # A flat table whose cells are merged. The publish splices the table back in, so the
    # colspan survives and the Markdown body is editable. First positive case.
    "flat merged cells": (
        "<p>prose one here</p><table><thead><tr><th>h</th></tr></thead>"
        '<tbody><tr><td colspan="2">a</td></tr></tbody></table>',
        "table-splice-v1",
    ),
    "flat row span": (
        '<table><thead><tr><th>h</th></tr></thead><tbody><tr><td rowspan="2">a</td></tr></tbody></table>',
        "table-splice-v1",
    ),
    # Presentation nobody has proven: a cell colour is not in any capability's code set.
    "cell colour": (
        '<table><tbody><tr><td style="background-color: rgb(255,0,0);"><p>c</p></td></tr></tbody></table>',
        None,
    ),
    # Explicitly excluded by the decision, and it shares almost every code with the
    # positive case -- which is why the discriminator is the path and not the code set.
    "table inside a table cell": (
        "<table><tbody><tr><td><table><tbody><tr><td>x</td></tr></tbody></table></td></tr></tbody></table>",
        None,
    ),
    # A Confluence macro is another unsupported owner boundary. The table may look flat,
    # but a table splice must not authorize Markdown edits inside the macro body.
    "table inside a macro": (
        '<ac:structured-macro ac:name="expand"><ac:rich-text-body><table><thead><tr><th>h</th></tr></thead>'
        '<tbody><tr><td colspan="2">a</td></tr></tbody></table></ac:rich-text-body></ac:structured-macro>',
        None,
    ),
}


@pytest.mark.parametrize("case", list(PRESERVATION_CASES), ids=list(PRESERVATION_CASES))
def test_the_second_axis_decides_the_write_for_an_unclassifiable_page(case: str, tmp_path: Path) -> None:
    """§8.2.1. The grade keeps its meaning; the capability decides the write.

    All four of these grade `xhtml_required`, which is the point: the grade says what
    regenerating from Markdown alone would lose, and that is true for every one of them.
    What differs is whether the managed publish path is *proven* to preserve it.
    """

    storage, _would_be = PRESERVATION_CASES[case]
    result, managed = _pull_result(storage, tmp_path)

    assert result.compatibility["status"] == "xhtml_required", "this page no longer grades as it did"
    # No capability is registered, so every one of these goes to the storage workflow --
    # §8.2 exactly as written. When one is registered this assertion is what has to change,
    # and it should change deliberately.
    assert result.compatibility["preservation_capability"] is None
    assert result.compatibility["canonical_write_permitted"] is False
    assert not managed.exists()


@pytest.mark.parametrize("case", list(PRESERVATION_CASES), ids=list(PRESERVATION_CASES))
def test_the_discriminator_tells_the_shapes_apart(case: str) -> None:
    """The registry is empty, so this exercises the rule directly against a registered one.

    Otherwise the discriminator ships untested: with no capability registered every page
    gets `None`, and a rule that always answers `None` is indistinguishable from a correct
    one. What is being checked is that a flat table with merged cells is covered, and that
    a cell colour and a table inside a table cell are not -- the last of which shares
    almost every diagnostic code with the first, which is why the paths decide it.
    """

    import atlassian_skills.confluence.preservation as module
    from atlassian_skills.confluence.compatibility import compatibility_payload

    storage, expected = PRESERVATION_CASES[case]
    findings = compatibility_payload("123", storage)["findings"]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, "CAPABILITIES", (module.TABLE_SPLICE_V1,))
        capability = module.preservation_for(findings, storage)

    assert (capability.name if capability is not None else None) == expected


def test_a_covered_page_says_what_it_may_not_edit() -> None:
    """A file editable in most respects and not in one is worse than one that is not
    editable at all, unless the exception is stated. Checked on the capability itself,
    since none is registered to check through a pull."""

    from atlassian_skills.confluence.preservation import TABLE_SPLICE_V1

    assert TABLE_SPLICE_V1.protects, "a capability that does not say what is protected is a trap"
    assert any("colspan" in item or "merged" in item for item in TABLE_SPLICE_V1.protects)


def test_a_page_with_no_capability_is_still_sent_to_the_storage_workflow(tmp_path: Path) -> None:
    """The refusal has to remain a route, not a dead end."""

    storage, _ = PRESERVATION_CASES["cell colour"]
    result, _managed = _pull_result(storage, tmp_path)

    assert result.status == "not_pulled"
    argvs = [action.get("argv", []) for action in result.compatibility["next_actions"]]
    assert any("xhtml" in argv for argv in argvs), argvs


def test_no_capability_ever_covers_a_content_code() -> None:
    """The boundary the whole decision rests on.

    Markdown's own loss must be explained; the actual candidate's loss must be prevented.
    Topology and presentation can be spliced back from the remote. Text cannot be assumed
    to survive, so no capability may claim it -- checked against the registry rather than
    against one page, because the next capability is the one that would get this wrong.
    """

    from atlassian_skills.confluence.preservation import CAPABILITIES, TABLE_SPLICE_V1

    for capability in {*CAPABILITIES, TABLE_SPLICE_V1}:
        offending = [code for code in capability.codes if code.endswith(("#text", "#content"))]
        assert not offending, f"{capability.name} claims content survives: {offending}"


def test_the_registry_capabilities_are_closed_by_a_real_contract_test() -> None:
    """A capability is a claim, and this is the receipt.

    §8.2.1 says a capability is closed by a contract test on the *same public push path*.
    A proof that runs against an internal helper proves nothing about what a user will do,
    so this checks both that the named test exists and that it goes through `push_md`.
    """

    from atlassian_skills.confluence.preservation import CAPABILITIES, TABLE_SPLICE_V1

    # Every defined capability, registered or not: the one that is defined and withheld is
    # exactly the one whose receipt should be checked, so that registering it is a
    # one-line change rather than a change plus a test nobody wrote.
    defined = {*CAPABILITIES, TABLE_SPLICE_V1}
    root = Path(__file__).resolve().parent
    sources = {path.name: path.read_text(encoding="utf-8") for path in root.glob("test_*.py")}
    for capability in defined:
        holders = [name for name, text in sources.items() if f"def {capability.closed_by}(" in text]
        assert holders, f"{capability.name} names {capability.closed_by}, which does not exist"
        text = sources[holders[0]]
        start = text.index(f"def {capability.closed_by}(")
        body = text[start : text.index("\ndef ", start + 1) if "\ndef " in text[start + 1 :] else len(text)]
        assert "push_md(" in body, f"{capability.closed_by} does not publish through the public push path"


def test_a_precomputed_artifact_is_reused_for_the_ragged_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """One preflight must not parse the same large remote page twice."""

    import cfxmark

    from atlassian_skills.confluence.compatibility import compatibility_payload

    storage = (
        "<table><thead><tr><th>key</th><th>value</th></tr></thead><tbody>"
        "<tr><td>a</td><td>b</td></tr>"
        "<tr><td>prefix</td><td>left</td><td>delta</td><td>right</td></tr>"
        "</tbody></table><p>After</p>"
    )
    artifact = cfxmark.to_md_artifact(
        storage,
        options=cfxmark.ConversionOptions(profile="editable"),
    )

    def unexpected_reparse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the supplied artifact must be reused")

    monkeypatch.setattr(cfxmark, "to_md_artifact", unexpected_reparse)

    payload = compatibility_payload("123", storage, base_artifact=artifact)

    assert payload["preservation_capability"] == "ragged-table-island-v1"
