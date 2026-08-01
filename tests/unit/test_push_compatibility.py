"""A push re-measures. It does not trust what the pull decided.

The assessment is a forecast about a page as it was when it was read. Remote
pages move -- someone edits one in the browser between a pull and a push -- and a
page that was `markdown_ready` when it was pulled can be `xhtml_required` by the
time anyone publishes it.

If the push reused the pull's answer, an approval granted for one document would
be applied to another. So the dry-run computes the assessment from the storage it
just fetched, and these tests pin that it is derived from the fresh remote body
rather than cached anywhere along the way.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from atlassian_skills.cli.main import app
from atlassian_skills.confluence.compatibility import compatibility_payload
from atlassian_skills.confluence.migration_preflight import build_managed_preflight
from tests.unit.conftest import pull_managed_accepting_named_losses
from tests.unit.test_state_free_body_write import BodyClient

runner = CliRunner()

CELL_BACKGROUND = (
    "<table><thead><tr><th>h</th></tr></thead>"
    '<tbody><tr><td data-highlight-colour="#ff0000">a</td></tr></tbody></table>'
)


def test_the_dry_run_reports_what_markdown_would_drop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = BodyClient()
    managed = tmp_path / "page.md"
    pull_managed_accepting_named_losses(client, "123", managed, no_assets=True)
    managed.write_text(managed.read_text(encoding="utf-8").replace("B", "Edited"), encoding="utf-8")
    monkeypatch.setattr("atlassian_skills.cli.confluence._make_client", lambda _ctx: client)

    result = runner.invoke(
        app,
        ["confluence", "page", "push-md", "123", "--md-file", str(managed), "--dry-run", "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["compatibility"]
    assert payload["schema"] == "atls-compatibility-v1"
    assert payload["status"]
    assert client.puts == 0


def test_the_assessment_is_derived_from_the_body_just_fetched(tmp_path: Path) -> None:
    """The property that keeps a stale approval from being reused.

    Asserted against `source_storage` -- the body this preflight itself read --
    rather than against a constant, so a refactor that starts caching the
    assessment on the managed file, or carrying it from the pull, breaks here
    instead of on someone's page.
    """

    client = BodyClient()
    managed = tmp_path / "page.md"
    pull_managed_accepting_named_losses(client, "123", managed, no_assets=True)
    managed.write_text(managed.read_text(encoding="utf-8").replace("B", "Edited"), encoding="utf-8")

    preflight = build_managed_preflight(client, "123", managed)
    assert preflight.to_dict()["compatibility"] == compatibility_payload(
        "123", preflight.source_storage, document_path=str(managed)
    )
    assert client.puts == 0


def test_two_different_remote_bodies_produce_two_different_answers(tmp_path: Path) -> None:
    """The same code path, two remotes, two classifications.

    If the assessment were pinned to anything but the live body, both of these
    would come back the same and the test above could pass while measuring a
    constant.
    """

    plain = BodyClient()
    plain_managed = tmp_path / "plain.md"
    pull_managed_accepting_named_losses(plain, "123", plain_managed, no_assets=True)

    lossy = BodyClient()
    lossy.storage = CELL_BACKGROUND
    lossy_managed = tmp_path / "lossy.md"
    pull_managed_accepting_named_losses(lossy, "123", lossy_managed, no_assets=True)

    plain_status = build_managed_preflight(plain, "123", plain_managed).to_dict()["compatibility"]["status"]
    lossy_status = build_managed_preflight(lossy, "123", lossy_managed).to_dict()["compatibility"]["status"]

    assert plain_status == "markdown_ready"
    assert lossy_status == "migration_required"
    assert plain.puts == 0 and lossy.puts == 0


def test_a_dry_run_never_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stated separately from the assertions above because it is the one property
    that must hold no matter what the assessment says."""

    client = BodyClient()
    client.storage = CELL_BACKGROUND
    managed = tmp_path / "page.md"
    pull_managed_accepting_named_losses(client, "123", managed, no_assets=True)
    monkeypatch.setattr("atlassian_skills.cli.confluence._make_client", lambda _ctx: client)

    result = runner.invoke(
        app,
        ["confluence", "page", "push-md", "123", "--md-file", str(managed), "--dry-run", "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    assert client.puts == 0


# --------------------------------------------------------------------------
# The forecast and the bill are different numbers
# --------------------------------------------------------------------------


def test_the_dry_run_separates_what_the_page_costs_from_what_this_edit_costs(
    tmp_path: Path,
) -> None:
    """The correction that keeps the guidance from over-blocking.

    A page whose cell carries a background classifies `migration_required` --
    regenerating it from scratch would drop the colour. But an edit to a
    paragraph splices the untouched table straight back in, so this publish drops
    nothing, and asking the author to approve a loss that is not going to happen
    is the same over-blocking this design set out to remove.

    Measured live: exactly that page published, and the background was still
    there afterwards.
    """

    client = BodyClient()
    client.storage = "<p>alpha paragraph text</p>" + CELL_BACKGROUND
    managed = tmp_path / "page.md"
    pull_managed_accepting_named_losses(client, "123", managed, no_assets=True)
    managed.write_text(
        managed.read_text(encoding="utf-8").replace("alpha paragraph", "alpha edited"),
        encoding="utf-8",
    )

    result = build_managed_preflight(client, "123", managed).to_dict()
    assert result["compatibility"]["status"] == "migration_required"
    assert result["compatibility"]["workflow_decision_required"] is True

    # ...and what this edit actually costs.
    assert result["candidate_loss"]["named_losses"] == []
    assert result["candidate_loss"]["requires_user_approval"] is False
    assert client.puts == 0


def test_an_edit_that_really_drops_a_named_loss_still_asks(tmp_path: Path) -> None:
    """Narrowed, not disarmed. The check is on the candidate, so a candidate that
    genuinely drops the background is still reported."""

    from atlassian_skills.confluence.compatibility import candidate_loss

    remote = "<p>alpha</p>" + CELL_BACKGROUND
    dropped = remote.replace(' data-highlight-colour="#ff0000"', "")
    report = candidate_loss(remote, dropped)
    assert report["requires_user_approval"] is True
    assert report["named_losses"][0]["code"] == "table-cell-background"


def test_deleting_the_cell_outright_is_not_a_named_loss() -> None:
    """The author asked for it to go. Reporting a deletion as a loss to approve is
    how a gate starts refusing the edits people actually make."""

    from atlassian_skills.confluence.compatibility import candidate_loss

    remote = "<p>alpha</p>" + CELL_BACKGROUND
    without_table = "<p>alpha</p>"
    assert candidate_loss(remote, without_table)["requires_user_approval"] is False


# --------------------------------------------------------------------------
# The presentation change the first publish makes, disclosed rather than hidden
# --------------------------------------------------------------------------
#
# `<p/>` between two ordinary body blocks is not a loss -- Confluence's editor
# replaces it with `<p><br/></p>` on a no-edit save, so it is not a state an author
# can hold. It is still a *change to how the page looks* until the platform catches
# up, and the author sees it on the first publish. Costing nothing is not the same
# as being invisible, so the receipt says so and says how many places.


def test_the_first_publish_discloses_the_presentation_change() -> None:
    from atlassian_skills.confluence.compatibility import candidate_loss

    loss = candidate_loss("<p>a</p><p /><p>b</p>", "<p>a</p><p><br /></p><p>b</p>")
    assert loss["first_publish_changes_presentation"] is True
    assert loss["affected_occurrences"] == 1
    # Disclosure *and* consent, since R4-pre. Nothing is lost, but the author's readers do
    # see different spacing, and the gate refuses without a fingerprint -- so this field says
    # the same thing the gate does. The earlier version of this line asserted `False`, which
    # is how the dry run came to contradict the push.
    assert loss["requires_user_approval"] is True


def test_a_publish_that_changes_no_presentation_says_so() -> None:
    """False rather than absent.

    An agent reading `.get("first_publish_changes_presentation")` cannot tell a
    build that has not got the field from a publish that changes nothing.
    """

    from atlassian_skills.confluence.compatibility import candidate_loss

    loss = candidate_loss("<p>a</p><p>b</p>", "<p>a</p><p>b</p>")
    assert loss["first_publish_changes_presentation"] is False
    assert loss["affected_occurrences"] == 0


def test_occurrences_are_counted_not_flagged() -> None:
    """ "Presentation changes somewhere" is not reviewable; three places is."""

    from atlassian_skills.confluence.compatibility import candidate_loss

    remote = "<p>a</p><p /><p>b</p><p /><p>c</p><p /><p>d</p>"
    candidate = remote.replace("<p />", "<p><br /></p>")
    assert candidate_loss(remote, candidate)["affected_occurrences"] == 3


@pytest.mark.parametrize(
    ("shape", "remote", "candidate"),
    [
        # Trailing: the one position the editor-save convergence has still not been
        # watched in. The leading position used to be here too and left on 2026-07-31,
        # when the plan owner saved two sandbox pages in the browser and both
        # converged -- so this case is what keeps the rule from reaching past its evidence
        # rather than a list of shapes somebody remembered.
        ("trailing", "<p>a</p><p />", "<p>a</p><p><br /></p>"),
        (
            "in a list item",
            "<ul><li><p>a</p><p /><p>b</p></li></ul>",
            "<ul><li><p>a</p><p><br /></p><p>b</p></li></ul>",
        ),
        (
            "in a table cell",
            "<table><tbody><tr><td><p>a</p><p /><p>b</p></td></tr></tbody></table>",
            "<table><tbody><tr><td><p>a</p><p><br /></p><p>b</p></td></tr></tbody></table>",
        ),
    ],
)
def test_an_empty_paragraph_outside_the_shape_is_not_disclosed_as_presentation(
    shape: str, remote: str, candidate: str
) -> None:
    """Outside the observed shape it is a named loss, and the two must not be reported as
    one thing. Getting this wrong relabels a real loss as cosmetic."""

    from atlassian_skills.confluence.compatibility import candidate_loss

    loss = candidate_loss(remote, candidate)
    assert loss["first_publish_changes_presentation"] is False, shape
    assert loss["affected_occurrences"] == 0, shape


# --------------------------------------------------------------------------
# One permission, read by the payload and the actions alike
# --------------------------------------------------------------------------


def test_the_actions_follow_the_same_permission_the_payload_reports(monkeypatch) -> None:
    """The payload and its own `next_actions` must not disagree about the workflow.

    `canonical_write_permitted` in the envelope is computed: for an `xhtml_required`
    page it is true exactly when a preservation capability covers the page. But
    `_next_actions` was handed the static policy row instead, where the same field is
    always `False` for that grade -- so a covered page would report "Markdown may write
    here" beside a list of storage-workflow commands and nothing else.

    Kept as an injected capability rather than borrowing the registered ragged-table
    one: this test is about action/payload agreement, independent of any real shape.
    """

    from atlassian_skills.confluence import compatibility as module
    from atlassian_skills.confluence.preservation import PreservationCapability

    covering = PreservationCapability(
        name="test-covers-everything-v1",
        codes=frozenset({"td@class", "td@style", "col@style", "table@class"}),
        protects=("table geometry",),
        closed_by="injected by this test to make the computed axis say yes",
    )
    monkeypatch.setattr(module, "preservation_for", lambda *args, **kwargs: covering)

    unclassifiable = '<table><tbody><tr><td class="x" style="width: 1px;"><p>cell</p></td></tr></tbody></table>'
    payload = module.compatibility_payload("1", unclassifiable, document_path="page.md")

    # Asserted, not skipped. `cfxmark` is pinned to one exact build, so this grade cannot
    # drift underneath the test without a dependency change -- and a dependency change that
    # moves it is the news, not a reason to stop checking. Skipping here retired the
    # assertion below silently the moment the fixture stopped grading as expected.
    assert payload["status"] == "xhtml_required", f"fixture no longer grades xhtml_required: {payload['status']}"
    assert payload["canonical_write_permitted"] is True

    labels = " ".join(action["label"] for action in payload["next_actions"])
    argvs = [action["argv"] for action in payload["next_actions"]]
    assert any("md" in argv and "push" in argv for argv in argvs), (
        f"payload permits a canonical write and offers no Markdown action: {labels}"
    )


# --------------------------------------------------------------------------
# A6: the identity capability, and what registering it is allowed to rest on
# --------------------------------------------------------------------------


def test_an_identity_bound_page_may_be_written_once_the_capability_is_registered() -> None:
    """§8.2's precondition, met rather than deferred.

    `markdown_identity_bound` was hardcoded to refuse the canonical write, because §8.2
    permits the grade only where an identity-preservation capability is statically
    registered for the converter and profile and closed by a contract test on the public
    push path. No registry existed, so the row said `False` with a comment promising it
    later -- and 14 of 55 live pages were written on a runtime judgement before that
    comment was honoured.

    The registry exists now, so what flips the row is the registry.
    """

    from atlassian_skills.confluence.compatibility import compatibility_payload

    macro = (
        '<ac:structured-macro ac:name="info" ac:schema-version="1" ac:macro-id="7f3a-0001">'
        "<ac:rich-text-body><p>note</p></ac:rich-text-body></ac:structured-macro>"
    )
    payload = compatibility_payload("1", f"<p>prose</p>{macro}", document_path="page.md")
    assert payload["status"] == "markdown_identity_bound"
    assert payload["canonical_write_permitted"] is True
    assert payload["identity_preservation_capability"] is not None


def test_the_capability_is_bound_to_the_converter_it_was_proven_against() -> None:
    """A cfxmark upgrade must not inherit the permission silently.

    The carry is cfxmark's behaviour, so the evidence is about a version of cfxmark. A
    registry entry that did not name one would keep granting the write across a converter
    change that altered exactly the mechanism it depends on -- and the failure is silent,
    because a detached macro renders identically.
    """

    import cfxmark

    from atlassian_skills.confluence.preservation import IDENTITY_PRESERVATION

    assert IDENTITY_PRESERVATION is not None
    assert IDENTITY_PRESERVATION.converter == f"cfxmark {cfxmark.__version__}"
    assert IDENTITY_PRESERVATION.profile == "editable"
    assert IDENTITY_PRESERVATION.closed_by
    assert IDENTITY_PRESERVATION.shapes


def test_a_capability_for_another_converter_does_not_grant_the_write(monkeypatch) -> None:
    """The version check can fail. Without this the binding is decoration."""

    from atlassian_skills.confluence import preservation as module
    from atlassian_skills.confluence.compatibility import compatibility_payload

    stale = module.IdentityPreservation(
        name="identity-carry-v1",
        converter="cfxmark 0.0.1",
        profile="editable",
        shapes=("a macro with a body",),
        closed_by="a test that ran against a different converter",
    )
    monkeypatch.setattr(module, "IDENTITY_PRESERVATION", stale)

    macro = (
        '<ac:structured-macro ac:name="info" ac:schema-version="1" ac:macro-id="7f3a-0001">'
        "<ac:rich-text-body><p>note</p></ac:rich-text-body></ac:structured-macro>"
    )
    payload = compatibility_payload("1", f"<p>prose</p>{macro}", document_path="page.md")
    assert payload["status"] == "markdown_identity_bound"
    assert payload["canonical_write_permitted"] is False
    assert payload["identity_preservation_capability"] is None


# --------------------------------------------------------------------------
# The identity capability must cover the page, not just the build
# --------------------------------------------------------------------------
#
# R4-pre P1: `IdentityPreservation.shapes` was documentation. The lookup checked only the
# converter and profile, so every `markdown_identity_bound` page on this build was granted a
# canonical write -- including identity-bearing structures nobody had proven a carry for.
# That is the same defect §8.2 exists to prevent, one level in: a name promising a check,
# with the check scoped to the wrong thing.

_MACRO = (
    '<ac:structured-macro ac:name="info" ac:schema-version="1" ac:macro-id="{mid}">'
    "<ac:rich-text-body><p>{body}</p></ac:rich-text-body></ac:structured-macro>"
)


def test_a_proven_shape_is_covered() -> None:
    from atlassian_skills.confluence.compatibility import compatibility_payload

    payload = compatibility_payload("1", "<p>prose</p>" + _MACRO.format(mid="7f3a-0001", body="note"))
    assert payload["status"] == "markdown_identity_bound"
    assert payload["canonical_write_permitted"] is True
    assert payload["identity_preservation_capability"] == "identity-carry-v1"


@pytest.mark.parametrize(
    ("shape", "grade", "storage"),
    [
        (
            # cfxmark's own harness calls this out: a macro in a table cell loses its body
            # under the `editable` profile, so a carry here was never proven.
            #
            # Refused one step earlier than the other two: the cell makes the page
            # unclassifiable, so it never reaches the identity gate at all. Recorded as its
            # own grade rather than smoothed over -- the outcome is the same refusal, but by
            # a different route, and a route that changes is worth seeing. A `pytest.skip`
            # on the grade mismatch used to hide this case entirely.
            "a macro inside a table cell",
            "xhtml_required",
            "<table><tbody><tr><td>" + _MACRO.format(mid="7f3a-0001", body="note") + "</td></tr></tbody></table>",
        ),
        (
            # Two macros the carry cannot tell apart: identical bodies, different ids.
            # `IDENTITY_CARRY`'s own docstring says nothing in a positional walk can say
            # which id belongs to which, and the registry must not claim otherwise.
            "two macros with identical bodies",
            "markdown_identity_bound",
            "<p>prose</p>" + _MACRO.format(mid="7f3a-0001", body="same") + _MACRO.format(mid="7f3a-0002", body="same"),
        ),
        (
            # An attachment reference carries `ri:filename` and `ri:version-at-save`, and the
            # live measurement behind this registry was about macro ids.
            "an attachment reference",
            "markdown_identity_bound",
            '<p>prose</p><p><ac:image><ri:attachment ri:filename="d.png" ri:version-at-save="3" /></ac:image></p>',
        ),
    ],
)
def test_an_unproven_identity_shape_is_not_covered(shape: str, grade: str, storage: str) -> None:
    """The capability must decline what it has not proven, even on the right build.

    The grade is asserted rather than skipped past. On a pinned converter it is
    deterministic, so a shape that lands somewhere else has either left this test's
    subject or found a real change -- both worth a failure. Skipped, the two assertions
    that carry the point stopped running and nothing said so.
    """

    from atlassian_skills.confluence.compatibility import compatibility_payload

    payload = compatibility_payload("1", storage)
    assert payload["status"] == grade, f"{shape} now grades {payload['status']}, not {grade}"
    assert payload["canonical_write_permitted"] is False, shape
    assert payload["identity_preservation_capability"] is None, shape


def test_covers_declines_an_unknown_finding_even_though_no_page_reaches_it() -> None:
    """Defence in depth, asked directly because the payload cannot reach it.

    An unknown finding grades a page `xhtml_required`, so the identity branch never sees one
    and no storage shape can exercise this clause. It stays because a carry proven against
    classified structures says nothing about an unclassified one, and the day some other
    caller reaches `covers` with a mixed finding list, "it was unreachable when written" is
    not a reason for it to say yes.
    """

    from atlassian_skills.confluence.preservation import IDENTITY_PRESERVATION

    assert IDENTITY_PRESERVATION is not None
    proven = [{"verdict": "identity_carry", "code": "ac:structured-macro@ac:macro-id", "count": 1}]
    one_macro = (
        '<ac:structured-macro ac:name="info" ac:macro-id="m1">'
        "<ac:rich-text-body><p>only macro</p></ac:rich-text-body>"
        "</ac:structured-macro>"
    )
    assert IDENTITY_PRESERVATION.covers(proven, one_macro) is True
    assert IDENTITY_PRESERVATION.covers([*proven, {"verdict": "unknown", "code": "td@style"}], one_macro) is False


def test_the_carry_cannot_be_granted_without_being_shown_the_page() -> None:
    """`storage` is required, because its default answered the ambiguity question wrongly.

    `covers` ends by asking whether the page holds two macros the positional carry cannot
    tell apart. That question needs the page. With `storage: str = ""` a caller who left
    it out got `_has_indistinguishable_macros("")` -> `False` -> "no ambiguity", so the
    carry was granted with the last check silently skipped -- a default that fails open on
    the one clause the live evidence says decides real pages.

    Both halves are asserted: omitting it raises, and supplying an actually ambiguous page
    declines. The second is what the first was hiding.
    """

    from atlassian_skills.confluence.preservation import IDENTITY_PRESERVATION

    assert IDENTITY_PRESERVATION is not None
    proven = [{"verdict": "identity_carry", "code": "ac:structured-macro@ac:macro-id", "count": 2}]

    with pytest.raises(TypeError):
        IDENTITY_PRESERVATION.covers(proven)  # type: ignore[call-arg]

    body = "<ac:rich-text-body><p>same words</p></ac:rich-text-body>"
    twins = (
        f'<ac:structured-macro ac:name="info" ac:macro-id="m1">{body}</ac:structured-macro>'
        f'<ac:structured-macro ac:name="info" ac:macro-id="m2">{body}</ac:structured-macro>'
    )
    assert IDENTITY_PRESERVATION.covers(proven, twins) is False


def test_the_dry_run_and_the_push_give_the_same_answer_about_consent() -> None:
    """R5-3: `publish_consent_required` said no while the push said yes.

    `candidate_loss.requires_user_approval` counted only named losses, so a presentation
    change reported `false` -- and the preflight exposed that value as
    `publish_consent_required`, which the Skill tells agents to branch on. Meanwhile the
    migration report said `consent_required: true` and the real push refused without a
    fingerprint.

    That is a public contract disagreeing with itself, not a usability wrinkle: an agent
    that trusts the dry run publishes nothing and cannot tell why. One field decides, and it
    is the one the gate uses.
    """

    from atlassian_skills.confluence.compatibility import candidate_loss

    presentation = candidate_loss("<p>a</p><p /><p>b</p>", "<p>a</p><p><br /></p><p>b</p>")
    assert presentation["first_publish_changes_presentation"] is True
    assert presentation["requires_user_approval"] is True, "the push refuses this without consent"

    nothing = candidate_loss("<p>a</p><p>b</p>", "<p>a</p><p>b</p>")
    assert nothing["requires_user_approval"] is False


def test_every_surface_agrees_that_an_exact_blank_marker_needs_no_consent(tmp_path: Path) -> None:
    """The marker removes the presentation change rather than waiving it.

    `candidate_loss` still requires approval when fed `<p/>` versus
    `<p><br/></p>` directly. The managed path now emits a marker that reconstructs
    `<p/>` exactly, so all public surfaces must say that no change and no consent
    exist.
    """

    from atlassian_skills.confluence.push_md import push_md

    client = BodyClient()
    client.storage = "<p>alpha paragraph text here</p><p /><p>bravo paragraph text here</p>"
    managed = tmp_path / "page.md"
    pull_managed_accepting_named_losses(client, "123", managed, no_assets=True)
    managed.write_text(managed.read_text(encoding="utf-8").replace("alpha paragraph", "alpha edited"), encoding="utf-8")

    preflight = build_managed_preflight(client, "123", managed)
    payload = preflight.to_dict()

    assert payload["candidate_loss"]["first_publish_changes_presentation"] is False
    assert payload["candidate_loss"]["named_losses"] == []
    assert payload["publish_consent_required"] is False
    assert preflight.consent_required is False
    assert payload["consent_required"] is False
    assert payload["migration_report"]["occurrences"] == []

    accepted = push_md(client, "123", managed.read_text(encoding="utf-8"), managed_path=managed)
    assert accepted["first_publish_changes_presentation"] is False
    assert client.storage == "<p>alpha edited text here</p><p/><p>bravo paragraph text here</p>"
    assert accepted["status"] in {"updated", "reconciled"}
    assert client.puts == 1


def test_publish_consent_required_is_the_gate_s_own_answer_on_every_trigger(tmp_path: Path) -> None:
    """The same defect as the test above, on the trigger that was not looked at.

    `publish_consent_required` was recomputed from `candidate_loss`, which counts named
    losses and presentation changes. Consent has a third trigger -- a migration
    occurrence -- and that one was never counted, so a page needing consent for an
    emoticon migration reported `false` while the gate held `true` and the push refused.

    SKILL.md tells an agent "`workflow_decision_required` chooses the representation;
    `publish_consent_required` approves this dry-run candidate's loss. Ask only for the
    latter." An agent that follows that reads `false`, does not ask, and is refused.

    The field is now the gate's own value rather than a second expression that has to be
    kept in step -- the same correction as reading `proof_mootness` instead of
    recomputing losslessness. Asserted as `True` on purpose: the previous fixture stopped
    producing a consent case when cfxmark 0.6.0 removed the empty-paragraph divergence,
    and with it went the only assertion that this field is ever `True`.
    """

    from tests.unit.managed_seam import pull_managed_suspending_the_write_policy
    from tests.unit.test_managed_error_redaction import ManagedClient

    client = ManagedClient('<p><ac:emoticon ac:name="smile"/></p><p>Base</p>')
    managed = tmp_path / "page.md"
    pull_managed_suspending_the_write_policy(client, "123", managed, no_assets=True)
    managed.write_text(managed.read_text(encoding="utf-8").replace("Base", "Edited"), encoding="utf-8")

    preflight = build_managed_preflight(client, "123", managed)
    payload = preflight.to_dict()

    assert preflight.consent_required is True
    assert payload["status"] == "migration_consent_required"
    assert [occurrence["consent_required"] for occurrence in payload["migration_report"]["occurrences"]] == [True]
    assert payload["publish_consent_required"] is True
    assert client.puts == 0


def test_an_uncovered_identity_page_does_not_recommend_a_workflow_it_will_not_write() -> None:
    """The payload said "use Markdown, nothing to decide" and then wrote no file.

    `markdown_identity_bound` grants a canonical write only where the registered carry
    covers *this page*. When it does not -- two macros with identical content signatures,
    which `covers()` refuses because nothing in a positional walk can say which id belongs
    to which -- `canonical_write_permitted` flips to `False` and every other field kept
    saying the opposite: `recommended_workflow: markdown`,
    `workflow_decision_required: False`, and a summary about publishing through the
    managed path.

    The preservation axis got this treatment when its capability applies; the identity
    axis never got the mirror for when its carry does not. An agent reading the fields it
    is told to read runs the Markdown pull and finds nothing on disk.
    """

    macro = (
        '<ac:structured-macro ac:name="info" ac:macro-id="{id}">'
        "<ac:rich-text-body><p>same body</p></ac:rich-text-body></ac:structured-macro>"
    )
    storage = "<p>alpha</p>" + macro.format(id="id-1") + macro.format(id="id-2")

    payload = compatibility_payload("1", storage)

    assert payload["status"] == "markdown_identity_bound"
    assert payload["canonical_write_permitted"] is False
    # The fields an agent branches on must agree with that.
    assert payload["workflow_decision_required"] is True
    assert payload["recommended_workflow"] != "markdown"
    assert payload["attention_reason"] == "identity_carry_not_proven_for_this_page"
