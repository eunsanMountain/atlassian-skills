"""The publish gate refuses on what this candidate would actually drop.

Not on the classification. That distinction is the whole design, and getting it
backwards would rebuild the problem this project exists to remove.

The classification answers "what would be lost if this page were regenerated from
its Markdown alone". Real publish paths do not regenerate from Markdown alone --
they splice the untouched parts of the remote back in -- so a page classified
`xhtml_required` can publish perfectly safely. Measured: a cell with
`colspan="2"` classifies as unknown, and the state-free publish keeps the colspan
because the table was never re-rendered.

Gating on the classification would refuse that publish. That is over-blocking,
which is what the ownership proof already does 55 times out of 55.

So the gate compares the candidate about to be written against the remote about to
be replaced, and refuses only what would genuinely be lost.

CORRECTION, 2026-07-29. This file first said the state-free path published a macro
page and dropped its id, and that line came from a run against the project's fake
client. The live server disagrees -- both paths carry the base forward, and the id
survives even an edit inside the macro's own body. The guard therefore has no
measured true positive, and the tests below say so rather than implying one.

What remains true is the invariant: publishing storage that drops identity the
remote holds detaches the page's macros, and leaves nothing to find afterwards.
The tests pin that the check fires on exactly that shape and on nothing else --
not a moved macro, not an inserted one, not a deliberate deletion.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from atlassian_skills.confluence.compatibility import candidate_loss, compatibility_payload
from atlassian_skills.confluence.identity_gate import (
    MACRO_ELEMENT,
    TRACKED,
    assert_identity_carried,
    find_identity_losses,
    find_rebound_attributes,
)
from atlassian_skills.confluence.pull_md import pull_md
from atlassian_skills.confluence.push_md import push_md
from atlassian_skills.core.errors import ValidationError
from tests.unit.managed_seam import pull_managed_suspending_the_write_policy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.unit.test_state_free_body_write import BodyClient  # noqa: E402

MACRO = (
    '<ac:structured-macro ac:name="info" ac:schema-version="1"{extra}>'
    "<ac:rich-text-body><p>note body</p></ac:rich-text-body>"
    "</ac:structured-macro>"
)
WITH_IDENTITY = "<p>prose one here</p>" + MACRO.format(extra=' ac:macro-id="7f3a-0001"') + "<p>prose two here</p>"


def _macro_ids(storage: str) -> list[str]:
    return re.findall(r'ac:macro-id="([^"]*)"', storage)


def _publish(storage: str, *, managed: bool) -> tuple[BodyClient, dict | None, Exception | None]:
    with tempfile.TemporaryDirectory() as directory:
        client = BodyClient()
        client.storage = storage
        managed_path = Path(directory) / "page.md"
        pull_managed_suspending_the_write_policy(client, "123", managed_path, no_assets=True)
        body = managed_path.read_text(encoding="utf-8").replace("prose one", "prose one edited")
        managed_path.write_text(body, encoding="utf-8")
        if not managed:
            body = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("<!--"))
        try:
            result = push_md(client, "123", body, **({"managed_path": managed_path} if managed else {}))
            return client, result, None
        except Exception as error:  # noqa: BLE001 - the refusal is the subject
            return client, None, error


# --------------------------------------------------------------------------
# The loss it exists to stop
# --------------------------------------------------------------------------


def test_a_candidate_that_drops_identity_is_refused() -> None:
    """The invariant, exercised on the one input that violates it.

    Reaching this shape needs the fake client: against a live server both publish
    paths carry the base forward and the id survives. So this is a test of the
    check, not a reproduction of a bug in the field -- and saying otherwise would
    put a story in the codebase that the next reader would trust."""

    client, result, error = _publish(WITH_IDENTITY, managed=False)
    assert result is None
    assert isinstance(error, ValidationError)
    assert error.context["reason"] == "identity_would_be_dropped"
    assert client.puts == 0


def test_the_refusal_says_what_to_do_without_inventing_a_path() -> None:
    """A refusal that does not say what to do instead is where callers start
    inventing flags -- but an argv with a hole in it is worse than none.

    This path is reached from a state-free write, so there is no managed file to
    name. It used to emit `--md-file <file>`, which cannot be run and puts the
    caller in exactly the position the run-what-is-returned rule forbids. It now
    says what to do in words and leaves the argv out."""

    _client, _result, error = _publish(WITH_IDENTITY, managed=False)
    assert isinstance(error, ValidationError)
    assert "next_action_argv" not in error.context
    assert "managed Markdown" in error.context["next_action_hint"]
    assert "<" not in error.context["next_action_hint"]


def test_the_managed_path_publishes_and_keeps_the_identity() -> None:
    """The gate must not become a blanket refusal. The managed path renders
    against the page it pulled, so the identity survives and the publish is
    exactly what the author asked for."""

    client, result, error = _publish(WITH_IDENTITY, managed=True)
    assert error is None, error
    assert result is not None
    assert client.puts == 1
    assert _macro_ids(client.storage) == ["7f3a-0001"]


def test_a_ragged_table_island_survives_the_public_managed_push(tmp_path: Path) -> None:
    """The registered preservation capability is a real pull/push contract.

    Markdown may edit prose outside the ragged table. The table's exact remote
    storage is carried forward, while editing inside it is rejected before PUT.
    """

    table = (
        "<table><thead><tr><th>key</th><th>value</th></tr></thead><tbody>"
        "<tr><td>a</td><td>b</td></tr>"
        "<tr><td>prefix</td><td>left</td><td>delta</td><td>right</td></tr>"
        "</tbody></table>"
    )
    storage = table + "<p>After</p>"
    client = BodyClient()
    client.storage = storage
    path = tmp_path / "page.md"

    pulled = pull_md(client, "123", output_path=path, portable=True, no_assets=True)

    assert pulled.compatibility["preservation_capability"] == "ragged-table-island-v1"
    assert pulled.compatibility["canonical_write_permitted"] is True
    assert pulled.compatibility["recommended_workflow"] == "markdown"
    assert pulled.compatibility["workflow_decision_required"] is False
    assert pulled.compatibility["attention_reason"] == "protected_remote_structures"
    assert all(action["argv"][:3] != ["confluence", "page", "xhtml"] for action in pulled.compatibility["next_actions"])
    assert all(item["kind"] != "in_place_blocked" for item in pulled.edit_guidance)
    body = path.read_text(encoding="utf-8").replace("After", "After edited")
    path.write_text(body, encoding="utf-8")
    result = push_md(client, "123", body, managed_path=path)
    assert result["status"] == "reconciled"
    assert client.puts == 1
    assert table in client.storage

    refusing_client = BodyClient()
    refusing_client.storage = storage
    refusing_path = tmp_path / "refusing.md"
    pull_md(refusing_client, "123", output_path=refusing_path, portable=True, no_assets=True)
    changed_table = refusing_path.read_text(encoding="utf-8").replace("delta", "changed")
    refusing_path.write_text(changed_table, encoding="utf-8")

    with pytest.raises(ValidationError) as raised:
        push_md(refusing_client, "123", changed_table, managed_path=refusing_path)

    assert refusing_client.puts == 0
    assert raised.value.context["reason"] == "protected_region_edited"
    assert raised.value.context["diagnostic_code"] == "protected-region-edited"


# --------------------------------------------------------------------------
# The over-blocking it must not cause
# --------------------------------------------------------------------------


def test_a_page_classified_unknown_still_publishes_when_nothing_is_lost() -> None:
    """The measurement that settled the design.

    `colspan` is unregistered, so a from-scratch regeneration cannot account for
    it and the page classifies as `xhtml_required`. The state-free publish splices
    the untouched table back in, so the colspan is still there afterwards.

    Refusing this on the strength of the classification would block a publish
    that loses nothing -- the exact over-blocking this project set out to remove.
    """

    storage = (
        "<p>prose one here</p>"
        '<table><thead><tr><th>h</th></tr></thead><tbody><tr><td colspan="2">alpha</td></tr></tbody></table>'
    )
    assert compatibility_payload("123", storage)["status"] == "xhtml_required"

    client, result, error = _publish(storage, managed=False)
    assert error is None, error
    assert result is not None
    assert client.puts == 1
    assert 'colspan="2"' in client.storage


def test_a_page_with_no_identity_at_all_is_untouched_by_the_gate() -> None:
    client, result, error = _publish("<p>prose one here</p><p>prose two here</p>", managed=False)
    assert error is None, error
    assert result is not None
    assert client.puts == 1


# --------------------------------------------------------------------------
# What "lost" means
# --------------------------------------------------------------------------


def test_a_macro_that_moved_has_not_lost_its_identity() -> None:
    """Compared as value sets, not positionally. Reordering two macros keeps both
    ids, and reporting a move as a loss would refuse an edit that is safe."""

    remote = MACRO.format(extra=' ac:macro-id="a"') + MACRO.format(extra=' ac:macro-id="b"')
    reordered = MACRO.format(extra=' ac:macro-id="b"') + MACRO.format(extra=' ac:macro-id="a"')
    assert find_identity_losses(remote, reordered) == ()


def test_an_inserted_macro_is_not_a_loss() -> None:
    """A macro just written has no id yet -- the server assigns one on save. Reading
    "no id" as "lost id" would refuse every insertion."""

    remote = MACRO.format(extra=' ac:macro-id="a"')
    assert find_identity_losses(remote, remote + MACRO.format(extra="")) == ()


def test_deleting_one_macro_and_inserting_another_is_not_a_detach() -> None:
    """The false positive that counting produced.

        remote     A(id=a)  B(id=b)
        candidate  A(id=a)  C(no id)

    Both sides hold two macros and the candidate holds one id, exactly as if A had
    kept its id and B had lost one. Counting refused this and blocked an ordinary
    edit. Matching by content tells them apart."""

    other = (
        '<ac:structured-macro ac:name="warning" ac:macro-id="b">'
        "<ac:rich-text-body><p>warn</p></ac:rich-text-body></ac:structured-macro>"
    )
    inserted = (
        '<ac:structured-macro ac:name="note"><ac:rich-text-body><p>fresh</p></ac:rich-text-body></ac:structured-macro>'
    )
    remote = MACRO.format(extra=' ac:macro-id="a"') + other
    candidate = MACRO.format(extra=' ac:macro-id="a"') + inserted
    assert find_identity_losses(remote, candidate) == ()


def test_two_identical_macros_where_one_lost_its_id_is_ambiguous() -> None:
    """Reported as undecidable rather than resolved. Choosing between two macros
    with identical content is how a comment thread lands on the wrong one."""

    remote = MACRO.format(extra=' ac:macro-id="a"') + MACRO.format(extra=' ac:macro-id="b"')
    candidate = MACRO.format(extra=' ac:macro-id="a"') + MACRO.format(extra="")
    (loss,) = find_identity_losses(remote, candidate)
    assert loss.ambiguous is True


def test_an_edited_macro_that_also_lost_its_id_is_not_decided_here() -> None:
    """The limit of content correspondence, pinned so nobody reads silence as
    safety.

        remote     macro, body "n",      id=a
        candidate  macro, body "edited", no id

    That is either an edit that dropped the id or a delete-and-rewrite, and the
    two documents do not distinguish them. This gate reports nothing rather than
    guessing.

    It is guarded elsewhere: the managed path holds the base artifact and the
    operation journal, so it knows which edit was actually made."""

    remote = MACRO.format(extra=' ac:macro-id="a"')
    edited_without_id = (
        '<ac:structured-macro ac:name="info" ac:schema-version="1">'
        "<ac:rich-text-body><p>note body edited</p></ac:rich-text-body></ac:structured-macro>"
    )
    assert find_identity_losses(remote, edited_without_id) == ()


def test_deleting_a_macro_on_purpose_is_not_a_detach() -> None:
    """The correction that a first version of this gate needed.

    Comparing identity as value sets refused this, because the deleted macro's id
    is genuinely missing from the candidate. But the macro is missing too -- the
    author asked for it to go, and its comments go with it. Refusing blocked a
    deliberate deletion on a path where it had always worked.

    What the gate is for is the other shape: the macro survives and its identity
    does not.
    """

    remote = MACRO.format(extra=' ac:macro-id="a"') + MACRO.format(extra=' ac:macro-id="b"')
    assert find_identity_losses(remote, MACRO.format(extra=' ac:macro-id="a"')) == ()


def test_a_deletion_does_not_cover_for_a_detach_alongside_it() -> None:
    """Two macros in, one out, and that one stripped of its id. The deletion is
    accounted for and the detach is still reported -- otherwise deleting anything
    would launder a silent detach of everything else."""

    remote = MACRO.format(extra=' ac:macro-id="a"') + MACRO.format(extra=' ac:macro-id="b"')
    (loss,) = find_identity_losses(remote, MACRO.format(extra=""))
    assert loss.attribute == "ac:macro-id"
    assert loss.detached == 1


@pytest.mark.parametrize("attribute", ["ac:macro-id", "ac:local-id"])
def test_every_scanned_attribute_is_one_confluence_uses_as_identity(attribute: str) -> None:
    """Pinned so the scan list cannot quietly grow into attributes that are
    re-emitted from the Markdown rather than dropped -- those would report
    differences that are not losses."""

    from cfxmark.compatibility import IDENTITY_BEARING

    assert ("ac:structured-macro", attribute) in IDENTITY_BEARING


def test_no_losses_means_no_exception() -> None:
    assert_identity_carried("123", WITH_IDENTITY, WITH_IDENTITY, workflow="stateless")


# --------------------------------------------------------------------------
# A swapped id is not a kept id
# --------------------------------------------------------------------------


def test_a_rebound_identity_is_reported_even_though_the_attribute_is_present() -> None:
    """Only the drop was being looked for, so a candidate replacing one id with
    another on the same macro reported no loss at all -- the attribute is there,
    so nothing counted as detached.

    The server treats an id it does not recognise as a new macro, so the swap
    detaches that macro's comments exactly as a drop does, with nothing to
    notice afterwards. Same consequence, and it was invisible."""

    remote = MACRO.format(extra=' ac:macro-id="a"')
    swapped = MACRO.format(extra=' ac:macro-id="zzz"')

    # The old check sees an attribute in both and reports nothing.
    assert find_identity_losses(remote, swapped) == ()

    rebound = find_rebound_attributes(remote, swapped, element=MACRO_ELEMENT, attributes=TRACKED)
    assert [item.attribute for item in rebound] == ["ac:macro-id"]
    assert rebound[0].detached == 1


def test_the_same_identity_kept_is_not_a_rebind() -> None:
    """The check has to be quiet on the ordinary case or it is just noise."""

    remote = MACRO.format(extra=' ac:macro-id="a"')
    assert find_rebound_attributes(remote, remote, element=MACRO_ELEMENT, attributes=TRACKED) == ()


def test_a_reorder_is_not_a_rebind() -> None:
    """Compared as multisets, so moving two macros past each other keeps both
    ids. Reporting a move as a rebind would refuse an edit that is safe --
    the same mistake `find_identity_losses` already avoids."""

    remote = MACRO.format(extra=' ac:macro-id="a"') + MACRO.format(extra=' ac:macro-id="b"')
    reordered = MACRO.format(extra=' ac:macro-id="b"') + MACRO.format(extra=' ac:macro-id="a"')
    assert find_rebound_attributes(remote, reordered, element=MACRO_ELEMENT, attributes=TRACKED) == ()


def test_a_dropped_identity_is_not_also_counted_as_a_rebind() -> None:
    """The two findings must not double-count the same macro: a drop is
    `find_dropped_attributes`'s to report, and reporting it twice would make a
    single detached macro read as two.

    TWO macros, deliberately. Written with one, the candidate side of that
    signature is empty, the `if not after` early return fires, and the branch
    that can double-count is never entered -- so the first version of this test
    asserted a guard that was never reached and passed while the bug was live.
    """

    remote = MACRO.format(extra=' ac:macro-id="a"') + MACRO.format(extra=' ac:macro-id="b"')
    one_stripped = MACRO.format(extra=' ac:macro-id="a"') + MACRO.format(extra="")

    assert find_rebound_attributes(remote, one_stripped, element=MACRO_ELEMENT, attributes=TRACKED) == ()
    reported = candidate_loss(remote, one_stripped)["identity"]
    assert [item["kind"] for item in reported] == ["dropped"]
    assert sum(item["count"] for item in reported) == 1


def test_deleting_one_of_two_identical_macros_is_not_a_rebind() -> None:
    """Macros with identical bodies share a content signature, so the remote id
    that goes away with a deleted one looks "missing" from the candidate.

    Asking which remote ids vanished answers the wrong question and reported the
    author's own deletion as a rebind. The question is which ids the CANDIDATE
    carries that the remote never issued -- and here it carries none."""

    remote = MACRO.format(extra=' ac:macro-id="a"') + MACRO.format(extra=' ac:macro-id="b"')
    deleted = MACRO.format(extra=' ac:macro-id="a"')

    assert find_rebound_attributes(remote, deleted, element=MACRO_ELEMENT, attributes=TRACKED) == ()
    assert find_identity_losses(remote, deleted) == ()


def test_two_macros_rebound_at_once_are_both_counted() -> None:
    """The cap must bound the count to what the remote had without hiding a
    second genuine rebind underneath it."""

    remote = MACRO.format(extra=' ac:macro-id="a"') + MACRO.format(extra=' ac:macro-id="b"')
    both = MACRO.format(extra=' ac:macro-id="y"') + MACRO.format(extra=' ac:macro-id="z"')

    rebound = find_rebound_attributes(remote, both, element=MACRO_ELEMENT, attributes=TRACKED)
    assert [item.attribute for item in rebound] == ["ac:macro-id"]
    assert rebound[0].detached == 2


# --------------------------------------------------------------------------
# A swapped id must not reach the server either
# --------------------------------------------------------------------------


def test_the_publish_gate_refuses_a_rebound_identity_not_only_a_dropped_one() -> None:
    """Detecting a rebind and refusing to publish one are different things.

    `find_rebound_attributes` reported `macro-id A -> B` while this guard, which
    is the last thing between a candidate and the server, asked only whether an
    id had been dropped. So the candidate walked through the guard whose entire
    purpose is to stop exactly that.

    The consequence is identical either way: Confluence treats an id it does not
    recognise as a new macro, and the comments attached to the old instance stop
    resolving with nothing on the rendered page to show for it.
    """

    remote = MACRO.format(extra=' ac:macro-id="a"')
    swapped = MACRO.format(extra=' ac:macro-id="zzz"')

    with pytest.raises(ValidationError) as caught:
        assert_identity_carried("123", remote, swapped, workflow="stateless")

    context = caught.value.context
    # Named apart from a drop: the two need different things from a reader --
    # one asks where the id went, the other where the new one came from.
    assert context["reason"] == "identity_would_be_rebound"
    assert [item["kind"] for item in context["identity"]] == ["rebound"]


def test_a_dropped_identity_keeps_the_reason_it_always_had() -> None:
    """Adding the second question must not rename the first answer. Anything
    already branching on `identity_would_be_dropped` keeps working."""

    remote = MACRO.format(extra=' ac:macro-id="a"')
    with pytest.raises(ValidationError) as caught:
        assert_identity_carried("123", remote, MACRO.format(extra=""), workflow="stateless")

    assert caught.value.context["reason"] == "identity_would_be_dropped"
    assert [item["kind"] for item in caught.value.context["identity"]] == ["dropped"]


def test_an_undecidable_correspondence_still_outranks_both() -> None:
    """When duplicate content makes the correspondence undecidable, saying which
    of drop or rebind happened is itself a guess. That case keeps its own name."""

    remote = MACRO.format(extra=' ac:macro-id="a"') + MACRO.format(extra=' ac:macro-id="b"')
    one_stripped = MACRO.format(extra=' ac:macro-id="a"') + MACRO.format(extra="")

    with pytest.raises(ValidationError) as caught:
        assert_identity_carried("123", remote, one_stripped, workflow="stateless")

    assert caught.value.context["reason"] == "identity_mapping_ambiguous"


def test_a_refused_publish_sends_no_write_at_all() -> None:
    """The guard has to fire before the PUT, not alongside it. A refusal that
    still wrote would be the worst of both -- the damage done and an error
    saying it was prevented."""

    client = BodyClient()
    client.storage = MACRO.format(extra=' ac:macro-id="a"')

    with pytest.raises(ValidationError):
        assert_identity_carried("123", client.storage, MACRO.format(extra=' ac:macro-id="zzz"'), workflow="stateless")

    assert client.puts == 0


def test_a_flat_table_with_merged_cells_survives_a_managed_publish(tmp_path: Path) -> None:
    """The contract that would close `table-splice-v1` (§8.2.1), and does not.

    A capability is a claim that the managed publish path preserves a structure Markdown
    cannot express, and §8.2.1 requires the claim to be proven on the path a user actually
    takes. This is that proof, and it fails -- so the capability is defined and not
    registered, and `xhtml_required` writes nothing.

    Kept, named after the capability, and asserting the *measured* matrix rather than the
    claim, for two reasons. The registry's closure test looks for a test with this name
    going through `push_md`, so the day the managed path stops refusing, registering the
    capability makes this test the receipt without anyone having to write one. And the
    difference between the two paths is the finding: what blocks these pages is the
    ownership proof, not §8.2.
    """

    from atlassian_skills.confluence.push_md import push_md

    storage = (
        "<p>prose one here</p>"
        '<table><thead><tr><th>h</th></tr></thead><tbody><tr><td colspan="2">alpha</td></tr></tbody></table>'
    )

    # The pull writes nothing for this page, so the file has to come from somewhere for the
    # push to be measurable at all. The write policy is suspended and nothing else is --
    # the proof reads the page's storage, not its grade.
    import atlassian_skills.confluence.managed_pull as managed_pull_module
    from atlassian_skills.confluence.compatibility import compatibility_payload

    def pullable(*args, **kwargs):
        return {**compatibility_payload(*args, **kwargs), "canonical_write_permitted": True}

    client = BodyClient()
    client.storage = storage
    managed = tmp_path / "page.md"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(managed_pull_module, "compatibility_payload", pullable)
        pulled = pull_managed_suspending_the_write_policy(client, "123", managed, no_assets=True)
    assert pulled.compatibility["status"] == "xhtml_required"

    body = managed.read_text(encoding="utf-8").replace("prose one", "prose one edited")
    managed.write_text(body, encoding="utf-8")

    # Managed: refused, nothing published.
    with pytest.raises(ValidationError) as refused:
        push_md(client, "123", body, managed_path=managed)
    assert refused.value.context["reason"] == "ownership_proof_invalid"
    assert client.puts == 0

    # Non-managed, same page and same edit: published, and the merged cell is still there.
    # This is the pair that makes the finding a finding rather than an opinion.
    stripped = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("<!--"))
    result = push_md(client, "123", stripped)

    assert result["status"] == "updated"
    assert 'colspan="2"' in client.storage, "the merged cell did not survive the publish"
    assert "prose one edited" in client.storage
    assert client.puts == 1


# --------------------------------------------------------------------------
# The identity the *remote* hands back, which the readback cannot see
# --------------------------------------------------------------------------
#
# The gate above refuses a candidate that would drop or rebind an id, and that is
# the whole pre-write story. Afterwards there is a second question nothing asked:
# did the ids we sent survive into what the server actually stored?
#
# Nothing could have noticed. The readback check converts both sides to Markdown and
# compares that, and Markdown does not carry `ac:macro-id` -- by design, it is not
# author content. So a server that re-assigned every id on the PUT produced a
# `reconciled` receipt.
#
# It is not hypothetical. Measured 2026-07-29: the server preserves a macro's id only
# while the body is byte-identical, and re-assigns it when the body changes. The
# managed path normally splices the untouched macro back verbatim, so ids survive --
# but an author editing text inside a macro is an ordinary thing to do, and that is
# exactly the case where every comment on that macro detaches with nothing said.

_ID_MACRO = (
    '<ac:structured-macro ac:name="info" ac:schema-version="1" ac:macro-id="{mid}">'
    "<ac:rich-text-body><p>note</p></ac:rich-text-body></ac:structured-macro>"
)


class ReassigningClient(BodyClient):
    """A server that hands back different macro ids than the ones it was sent."""

    def __init__(self, *, reassign: bool, storage: str) -> None:
        super().__init__()
        self.reassign = reassign
        self.storage = storage

    def update_page(self, **kwargs: Any) -> dict[str, Any]:
        result = super().update_page(**kwargs)
        if self.reassign:
            self.storage = re.sub(r'ac:macro-id="[^"]*"', 'ac:macro-id="zzzz-9999"', self.storage)
        return result


def _managed_edit(client: BodyClient, tmp_path: Path) -> Path:
    path = tmp_path / "page.md"
    pull_managed_suspending_the_write_policy(client, "123", path, no_assets=True)
    client.gets = 0
    path.write_text(path.read_text(encoding="utf-8").replace("Base", "Changed"), encoding="utf-8")
    return path


def test_a_remote_that_reassigns_identity_is_reported_not_reconciled(tmp_path: Path) -> None:
    storage = "<p>Base</p>" + _ID_MACRO.format(mid="aaaa-0001") + "<p>Tail</p>"
    client = ReassigningClient(reassign=True, storage=storage)
    path = _managed_edit(client, tmp_path)

    result = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    # The write happened and cannot be taken back, so this is a report rather than a
    # refusal -- but it must not read as an unqualified success.
    assert result["remote_reassigned_identity"] == {"ac:macro-id": 1}
    assert result["status"] == "reconciled_identity_reassigned"


def test_a_remote_that_keeps_identity_says_so_rather_than_staying_silent(tmp_path: Path) -> None:
    """`{}` rather than a missing key, for the reason every other flag here is
    explicit: absent reads as "this build does not check" and that is the state this
    was in."""

    storage = "<p>Base</p>" + _ID_MACRO.format(mid="aaaa-0001") + "<p>Tail</p>"
    client = ReassigningClient(reassign=False, storage=storage)
    path = _managed_edit(client, tmp_path)

    result = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    assert result["status"] == "reconciled"
    assert result["remote_reassigned_identity"] == {}


def test_counts_are_checked_not_only_values(tmp_path: Path) -> None:
    """The same id sent twice and stored once. A set comparison sees nothing at all.

    This shape is the one cfxmark's own `identity_carry` docstring warns about: two
    macros the carry cannot tell apart can end up bound to one remote id. If the server
    then keeps it on only one of them, every value in the readback is a value we sent
    and the *set* of ids is unchanged -- so only the count says a macro was detached.

    The first version of this test used two DIFFERENT ids and dropped one, which a set
    comparison also catches. It passed against the mutation that replaced the multiset
    difference with a set difference, so it was proving the wrong thing.
    """

    storage = (
        "<p>Base</p>"
        + _ID_MACRO.format(mid="aaaa-0001").replace("<p>note</p>", "<p>first</p>")
        + _ID_MACRO.format(mid="aaaa-0001").replace("<p>note</p>", "<p>second</p>")
        + "<p>Tail</p>"
    )
    client = ReassigningClient(reassign=False, storage=storage)
    path = _managed_edit(client, tmp_path)
    # One macro keeps the id, the other loses it. Both macros are still there, so the
    # readback still matches as Markdown and the publish still reports success.
    client.on_readback = lambda: setattr(client, "storage", client.storage.replace(' ac:macro-id="aaaa-0001"', "", 1))

    result = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    assert result["remote_reassigned_identity"] == {"ac:macro-id": 1}
    assert result["status"] == "reconciled_identity_reassigned"


def test_a_merged_cell_table_that_projects_as_ragged_is_covered_and_preserved(tmp_path: Path) -> None:
    """The shape the registry comment said it did not promote, and does.

    A `colspan` row and a genuinely ragged row are indistinguishable in the Markdown
    projection -- both give `ragged_protected_table_paths` a row-width set of more than
    one -- so a flat merged-cell table is admitted by `ragged-table-island-v1`. The
    comment beside `CAPABILITIES` claimed the opposite.

    Measured before deciding which half to change: the island is carried through byte for
    byte, `colspan="2"` included, and an edit inside it is still refused. The capability
    is doing exactly what it promises on this shape, so the scope statement was what was
    wrong. This test closes the shape rather than leaving it admitted by accident, which
    is the same standard §8.2.1 asks of every capability.

    Note this is a stronger result than the one that kept `TABLE_SPLICE_V1` unregistered:
    there, a prose edit beside a merged-cell table was refused by the managed path. The
    island mechanism carries it where the bare code-set shape could not.
    """

    table = '<table><tbody><tr><td colspan="2">wide</td></tr><tr><td>a</td><td>b</td></tr></tbody></table>'
    storage = table + "<p>After</p>"
    client = BodyClient()
    client.storage = storage
    path = tmp_path / "page.md"

    pulled = pull_md(client, "123", output_path=path, portable=True, no_assets=True)
    assert pulled.compatibility["preservation_capability"] == "ragged-table-island-v1"
    assert pulled.compatibility["canonical_write_permitted"] is True

    body = path.read_text(encoding="utf-8").replace("After", "After edited")
    path.write_text(body, encoding="utf-8")
    result = push_md(client, "123", body, managed_path=path)

    assert result["status"] == "reconciled"
    assert client.puts == 1
    # The whole island, unchanged -- which is the promise, and `colspan` is the part of it
    # a Markdown regeneration could not have rebuilt.
    assert table in client.storage
    assert 'colspan="2"' in client.storage

    # And the refusing half, on the same shape.
    refusing = BodyClient()
    refusing.storage = storage
    refusing_path = tmp_path / "refusing.md"
    pull_md(refusing, "123", output_path=refusing_path, portable=True, no_assets=True)
    edited = refusing_path.read_text(encoding="utf-8").replace("wide", "widened")
    refusing_path.write_text(edited, encoding="utf-8")

    with pytest.raises(ValidationError) as raised:
        push_md(refusing, "123", edited, managed_path=refusing_path)

    assert refusing.puts == 0
    assert raised.value.context["reason"] == "protected_region_edited"


def test_nested_tables_are_refused_and_by_which_clause() -> None:
    """A table inside a table gets no capability, and the reason is recorded.

    `preservation_for` has two refusals that both catch this shape. `_CONTENT_CODE` refuses
    any page whose unknowns describe text, and `_nests_a_table` refuses a finding whose path
    crosses two tables. The second was written as the discriminator -- a nested table and a
    flat merged one produce nearly the same code set, so nothing but the paths tells them
    apart -- and on today's converter it never runs, because a nested table always drags a
    `td#text` along and the content clause returns one line earlier.

    Both facts are asserted. If cfxmark stops emitting `td#text` for a nested table the
    second assertion goes red, which is the moment the path clause becomes load-bearing and
    somebody should know it. If the path clause were deleted as dead code, the third goes
    red instead.
    """

    import cfxmark

    from atlassian_skills.confluence.preservation import (
        _CONTENT_CODE,
        _nests_a_table,
        preservation_for,
        unknown_codes,
    )

    inner = "<table><tbody><tr><td>inner</td></tr></tbody></table>"
    shapes = {
        "bare nesting": f"<table><tbody><tr><td>{inner}</td></tr></tbody></table>",
        "nested inside a ragged outer": (
            f"<table><tbody><tr><td>a</td><td>b</td></tr><tr><td>{inner}</td></tr></tbody></table>"
        ),
        "nested with styled cells": (f'<table><tbody><tr><td class="x">{inner}</td></tr></tbody></table>'),
        "nested with an empty inner cell": (
            "<table><tbody><tr><td><table><tbody><tr><td></td></tr></tbody></table></td></tr></tbody></table>"
        ),
    }

    for name, storage in shapes.items():
        findings = compatibility_payload("1", storage)["findings"]
        codes = unknown_codes(findings)
        assert codes, f"{name}: produced no unknown findings, so this fixture no longer tests anything"

        # 1. The outcome. No capability, whichever clause got there first.
        assert (
            preservation_for(
                findings,
                storage,
                converter=f"cfxmark {cfxmark.__version__}",
                profile="editable",
            )
            is None
        ), name

        # 2. Which clause actually decides today.
        assert any(_CONTENT_CODE.search(code) for code in codes), (
            f"{name}: no content code, so `_nests_a_table` is now the only thing refusing this "
            "page -- the path clause has become load-bearing and its docstring says otherwise"
        )

        # 3. And the clause held in reserve still recognises the shape.
        assert _nests_a_table(findings) is True, name
