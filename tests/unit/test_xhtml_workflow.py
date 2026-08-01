"""Editing a page as storage, for the documents Markdown cannot hold.

The advice to "use XHTML" was already correct and already useless: the low-level
read and write existed, and everything around them did not, so a caller who
reached this point composed commands out of parts of an error message.

The tests are ordered by what they cost when wrong. Publishing bytes nobody
approved is worst. Two representations both believing they may publish is next --
that is the conflict this design set out to remove, returning under a new name.
Then the offline checks, which exist so an editing mistake is caught before a
round trip rather than after a page renders wrong.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from atlassian_skills.confluence.sidecar import read_authority, sidecar_path
from atlassian_skills.confluence.xhtml_workflow import (
    diff_xhtml,
    pull_xhtml,
    push_xhtml,
    set_authority,
    validate_xhtml,
)
from atlassian_skills.core.errors import StaleError, ValidationError
from tests.unit.managed_seam import pull_managed_suspending_the_write_policy

MACRO = (
    '<ac:structured-macro ac:name="info" ac:macro-id="abc-123">'
    "<ac:rich-text-body><p>notice text here</p></ac:rich-text-body>"
    "</ac:structured-macro>"
)
STORAGE = f"<p>alpha paragraph text here</p>{MACRO}"


class StorageClient:
    """A page that holds storage, counts writes, and normalises on save.

    The normalisation is not decoration: Confluence rewrites what it is given,
    and code that assumes the body it sent is the body on the page records a
    hash of a document that does not exist.
    """

    base_url = "https://example.com/confluence"

    def __init__(self, storage: str = STORAGE) -> None:
        self.storage = storage
        self.version = 4
        self.puts = 0
        self.normalize = False

    def get_page(self, page_id: str, *args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            id=page_id,
            title="Page",
            body_storage=self.storage,
            version=SimpleNamespace(number=self.version),
        )

    def update_page(self, *, page_id: str, title: str, body: str, version_number: int, **kwargs: object) -> dict:
        self.puts += 1
        self.version = version_number
        self.storage = body + "<!-- server -->" if self.normalize else body
        return {"id": page_id, "version": {"number": version_number}}


def _pulled(tmp_path: Path, storage: str = STORAGE) -> tuple[StorageClient, Path]:
    client = StorageClient(storage)
    path = tmp_path / "page.xhtml"
    pull_xhtml(client, "123", output_path=path)
    return client, path


def _approved(client: StorageClient, path: Path) -> str:
    return str(push_xhtml(client, "123", path, dry_run=True)["candidate_sha256"])


# --------------------------------------------------------------------------
# Publishing only what was approved
# --------------------------------------------------------------------------


def test_a_push_without_the_candidate_hash_is_refused(tmp_path: Path) -> None:
    """Approval is for bytes, not for an intention. A storage document can be
    edited between the dry run someone read and the push that publishes, and the
    hash is the only thing that notices."""

    client, path = _pulled(tmp_path)
    path.write_text(STORAGE.replace("alpha paragraph", "alpha edited"), encoding="utf-8")

    with pytest.raises(ValidationError) as refused:
        push_xhtml(client, "123", path)
    assert refused.value.context["reason"] == "candidate_not_approved"
    assert client.puts == 0


def test_a_stale_candidate_hash_does_not_publish_the_current_file(tmp_path: Path) -> None:
    """The failure the hash exists for: approve one document, publish another.
    Without this the approval would carry over to whatever the file says now."""

    client, path = _pulled(tmp_path)
    path.write_text(STORAGE.replace("alpha paragraph", "alpha reviewed"), encoding="utf-8")
    approved = _approved(client, path)
    path.write_text(STORAGE.replace("alpha paragraph", "alpha changed afterwards"), encoding="utf-8")

    with pytest.raises(ValidationError) as refused:
        push_xhtml(client, "123", path, accept_candidate=approved)
    assert refused.value.context["reason"] == "candidate_not_approved"
    assert client.puts == 0


def test_the_approved_document_publishes_and_the_file_follows_the_readback(tmp_path: Path) -> None:
    """The server rewrites storage on save. If the sent bytes stayed on disk,
    every later diff would show a difference nobody made and an author could not
    tell their own pending edit from the server's housekeeping -- so the file and
    the record both follow what the page actually holds."""

    client, path = _pulled(tmp_path)
    client.normalize = True
    path.write_text(STORAGE.replace("alpha paragraph", "alpha edited"), encoding="utf-8")

    result = push_xhtml(client, "123", path, accept_candidate=_approved(client, path))
    assert result["status"] == "updated"
    assert result["server_normalized"] is True
    assert client.puts == 1
    assert path.read_text(encoding="utf-8") == client.storage

    # The proof it followed: pushing again publishes nothing and the page is not
    # reported as having moved.
    assert push_xhtml(client, "123", path, dry_run=True)["would_update"] is False
    assert client.puts == 1


def test_a_page_that_moved_underneath_is_refused(tmp_path: Path) -> None:
    client, path = _pulled(tmp_path)
    path.write_text(STORAGE.replace("alpha paragraph", "alpha edited"), encoding="utf-8")
    approved = _approved(client, path)
    client.storage = STORAGE.replace("notice text", "someone else edited")
    client.version += 1

    with pytest.raises(StaleError) as refused:
        push_xhtml(client, "123", path, accept_candidate=approved)
    assert refused.value.context["reason"] == "remote_stale"
    assert client.puts == 0


def test_a_dry_run_hands_back_a_command_that_carries_its_own_approval(tmp_path: Path) -> None:
    """A caller that has to assemble the flag from the payload will eventually
    assemble it wrong, and the wrong version of this flag publishes an unread
    document."""

    client, path = _pulled(tmp_path)
    path.write_text(STORAGE.replace("alpha paragraph", "alpha edited"), encoding="utf-8")

    result = push_xhtml(client, "123", path, dry_run=True)
    (action,) = result["next_actions"]
    argv = action["argv"]
    assert argv[argv.index("--accept-candidate") + 1] == result["candidate_sha256"]
    assert argv[argv.index("--if-version") + 1] == str(client.version)
    assert action["requires_user_approval"] is True
    assert client.puts == 0


def test_an_unchanged_document_does_not_make_a_version(tmp_path: Path) -> None:
    """A version whose only change is that someone pressed save is noise in a
    history other people read."""

    client, path = _pulled(tmp_path)
    assert push_xhtml(client, "123", path, accept_candidate=_approved(client, path))["status"] == "unchanged"
    assert client.puts == 0


# --------------------------------------------------------------------------
# Only one representation publishes
# --------------------------------------------------------------------------


def test_pulling_as_storage_makes_storage_authoritative(tmp_path: Path) -> None:
    _, path = _pulled(tmp_path)
    assert read_authority(path) == "xhtml"


def test_a_markdown_push_is_refused_while_storage_is_authoritative(tmp_path: Path) -> None:
    """The whole point of declaring authority. Publishing the Markdown copy would
    write back a rendering of a document Markdown was found unable to hold --
    the loss this path exists to refuse, arriving by the other door."""

    from atlassian_skills.confluence.migration_preflight import build_managed_preflight
    from atlassian_skills.confluence.pull_md import pull_md
    from tests.unit.test_state_free_body_write import BodyClient

    client = BodyClient()
    managed = tmp_path / "page.md"
    pull_md(client, "123", output_path=managed, portable=True, no_assets=True)
    set_authority("123", to="xhtml", md_path=managed)

    with pytest.raises(ValidationError) as refused:
        build_managed_preflight(client, "123", managed)
    assert refused.value.context["reason"] == "xhtml_is_authoritative"
    assert client.puts == 0


def test_handing_authority_back_lets_markdown_publish_again(tmp_path: Path) -> None:
    """A page reclassified as loss-free should not stay locked out of the
    workflow it can now use. One-way would be a trap dressed as a safeguard."""

    from atlassian_skills.confluence.migration_preflight import build_managed_preflight
    from atlassian_skills.confluence.pull_md import pull_md
    from tests.unit.test_state_free_body_write import BodyClient

    client = BodyClient()
    managed = tmp_path / "page.md"
    pull_md(client, "123", output_path=managed, portable=True, no_assets=True)
    # The storage copy has to be named to hand authority *to* Markdown: §10.2 forbids
    # granting that permission without re-grading the page, and the storage is what gets
    # graded. Going to `xhtml` needs nothing, because it only removes permission.
    exact = tmp_path / "page.xhtml"
    pull_xhtml(client, "123", output_path=exact)
    set_authority("123", to="xhtml", md_path=managed)
    set_authority("123", to="markdown", md_path=managed, xhtml_path=exact)

    build_managed_preflight(client, "123", managed)
    assert client.puts == 0


def test_a_storage_push_is_refused_while_markdown_is_authoritative(tmp_path: Path) -> None:
    """Symmetric, and the reason the check is on both sides. One-sided
    enforcement means the pair can still disagree -- just more quietly."""

    client, path = _pulled(tmp_path)
    set_authority("123", to="markdown", xhtml_path=path)
    path.write_text(STORAGE.replace("alpha paragraph", "alpha edited"), encoding="utf-8")

    with pytest.raises(ValidationError) as refused:
        push_xhtml(client, "123", path, accept_candidate="sha256:whatever")
    assert refused.value.context["reason"] == "markdown_is_authoritative"
    assert client.puts == 0


def test_changing_authority_needs_the_files_it_changes(tmp_path: Path) -> None:
    """Nothing here maps a page id to the documents pulled from it. A command
    that claimed to switch authority while leaving a sidecar behind would be
    worse than no command: the file it missed goes on believing it may
    publish."""

    with pytest.raises(ValidationError) as refused:
        set_authority("123", to="xhtml")
    assert refused.value.context["reason"] == "authority_target_required"


def test_a_record_from_another_page_does_not_have_its_authority_changed(tmp_path: Path) -> None:
    _, path = _pulled(tmp_path)
    side = sidecar_path(path)
    side.write_text(side.read_text(encoding="utf-8").replace('"123"', '"999"'), encoding="utf-8")

    with pytest.raises(ValidationError) as refused:
        set_authority("123", to="markdown", xhtml_path=path)
    assert refused.value.context["reason"] == "sidecar_page_mismatch"


# --------------------------------------------------------------------------
# The offline checks
# --------------------------------------------------------------------------


def test_an_unclosed_tag_is_caught_without_asking_the_server(tmp_path: Path) -> None:
    _, path = _pulled(tmp_path)
    path.write_text("<p>alpha paragraph text here", encoding="utf-8")

    with pytest.raises(ValidationError) as refused:
        validate_xhtml(path)
    assert refused.value.context["reason"] == "xhtml_not_well_formed"


def test_a_macro_that_lost_its_id_is_reported(tmp_path: Path) -> None:
    """The failure a text editor makes easy and a rendered page never shows: the
    macro still works, and the comments and permissions attached to it are gone."""

    _, path = _pulled(tmp_path)
    path.write_text(STORAGE.replace(' ac:macro-id="abc-123"', ""), encoding="utf-8")

    (finding,) = validate_xhtml(path)["findings"]
    assert finding["code"] == "identity_dropped"
    assert finding["attribute"] == "ac:macro-id"


def test_deleting_a_macro_outright_is_not_a_finding(tmp_path: Path) -> None:
    """The author asked for it to go. Reporting a deletion as a loss is how a
    check starts refusing the edits people actually make."""

    _, path = _pulled(tmp_path)
    path.write_text("<p>alpha paragraph text here</p>", encoding="utf-8")
    assert validate_xhtml(path)["findings"] == []


def test_an_undeclared_prefix_is_reported_rather_than_declared(tmp_path: Path) -> None:
    """A prefix we do not know is a part of the page we cannot reason about.
    Declaring it silently would turn an unknown into something that merely
    parses."""

    _, path = _pulled(tmp_path)
    path.write_text('<p>alpha</p><zz:thing xmlns:zz="urn:x">body</zz:thing>', encoding="utf-8")

    codes = {finding["code"] for finding in validate_xhtml(path)["findings"]}
    assert "undeclared_namespace" in codes


def test_validation_without_a_base_says_the_check_did_not_run(tmp_path: Path) -> None:
    """Reporting "nothing was dropped" from a comparison that never happened is
    worse than reporting that it never happened."""

    _, path = _pulled(tmp_path)
    sidecar_path(path).unlink()

    result = validate_xhtml(path)
    assert result["identity_checked"] is False
    assert result["findings"][0]["code"] == "identity_check_skipped"


# --------------------------------------------------------------------------
# Reading what is there
# --------------------------------------------------------------------------


def test_pull_writes_the_bytes_the_server_sent(tmp_path: Path) -> None:
    """Byte-preserving because this file is what gets published. A document that
    has been through a formatter is not the document that was read."""

    client, path = _pulled(tmp_path)
    assert path.read_text(encoding="utf-8") == client.storage


def test_a_diff_names_the_version_and_the_hash_to_publish_against(tmp_path: Path) -> None:
    client, path = _pulled(tmp_path)
    path.write_text(STORAGE.replace("alpha paragraph", "alpha edited"), encoding="utf-8")

    result = diff_xhtml(client, "123", path)
    assert result["identical"] is False
    assert "alpha edited" in result["diff"]
    assert result["remote_version"] == client.version
    assert result["candidate_sha256"] != result["remote_storage_sha256"]


# --------------------------------------------------------------------------
# Authority covers the page's copies, not just the file in hand
# --------------------------------------------------------------------------


def _managed(tmp_path: Path, client) -> Path:
    from atlassian_skills.confluence.pull_md import pull_md

    managed = tmp_path / "page.md"
    pull_md(client, "123", output_path=managed, portable=True, no_assets=True)
    return managed


def test_pulling_as_storage_takes_authority_off_the_markdown_copy(tmp_path: Path) -> None:
    """The defect a reviewer found: authority was written only beside the file
    just created, so `page.md.atls.json` still said `markdown` and both copies
    could publish. The skill promised the opposite, which made the documentation
    wrong rather than merely incomplete."""

    from tests.unit.test_state_free_body_write import BodyClient

    client = BodyClient()
    managed = _managed(tmp_path, client)
    assert read_authority(managed) == "markdown"

    result = pull_xhtml(client, "123", output_path=tmp_path / "page.xhtml")

    assert read_authority(managed) == "xhtml"
    assert [entry["path"] for entry in result["authority_transferred"]] == [str(managed)]


def test_a_markdown_push_is_refused_after_a_storage_pull_beside_it(tmp_path: Path) -> None:
    """Stated end to end rather than on the marker, because the marker is only
    worth having if the push consults it."""

    from atlassian_skills.confluence.migration_preflight import build_managed_preflight
    from tests.unit.test_state_free_body_write import BodyClient

    client = BodyClient()
    managed = _managed(tmp_path, client)
    pull_xhtml(client, "123", output_path=tmp_path / "page.xhtml")
    managed.write_text(managed.read_text(encoding="utf-8").replace("Base", "Edited"), encoding="utf-8")

    with pytest.raises(ValidationError) as refused:
        build_managed_preflight(client, "123", managed)
    assert refused.value.context["reason"] == "xhtml_is_authoritative"
    assert client.puts == 0


def test_handing_authority_back_moves_both_records(tmp_path: Path) -> None:
    from tests.unit.test_state_free_body_write import BodyClient

    client = BodyClient()
    managed = _managed(tmp_path, client)
    xhtml = tmp_path / "page.xhtml"
    pull_xhtml(client, "123", output_path=xhtml)

    # `xhtml_path` is what gets graded, and the sweep still moves both records.
    set_authority("123", to="markdown", md_path=managed, xhtml_path=xhtml)

    assert read_authority(managed) == "markdown"
    assert read_authority(xhtml) == "markdown"


def test_a_record_for_a_different_page_is_not_swept_up(tmp_path: Path) -> None:
    """The sweep is by page, not by directory. Moving a neighbour's authority
    because it happened to share a folder would be a worse bug than the one this
    fixes."""

    from tests.unit.test_state_free_body_write import BodyClient

    client = BodyClient()
    other = tmp_path / "other.xhtml"
    pull_xhtml(client, "999", output_path=other)
    set_authority("999", to="markdown", xhtml_path=other)

    pull_xhtml(client, "123", output_path=tmp_path / "page.xhtml")
    assert read_authority(other) == "markdown"


def test_the_payload_says_how_far_the_marker_reaches(tmp_path: Path) -> None:
    """A copy in another directory is not reachable from anything on this
    machine, so the contract is stated in the payload rather than implied.
    Silence here would read as "the whole page is covered", which is the claim
    this cannot make -- what backstops it is the push re-measuring fresh remote
    compatibility and candidate loss every time."""

    from tests.unit.test_state_free_body_write import BodyClient

    client = BodyClient()
    xhtml = tmp_path / "page.xhtml"
    pull_xhtml(client, "123", output_path=xhtml)

    assert set_authority("123", to="markdown", xhtml_path=xhtml)["scope"] == "files_named_and_their_directory"


# --------------------------------------------------------------------------
# §10.2: authority is not a way around the grade
# --------------------------------------------------------------------------


def test_authority_cannot_be_granted_to_markdown_without_a_gradeable_copy(tmp_path: Path) -> None:
    """The direction that grants permission is the one that has to prove something.

    Measured before this existed: `set-authority --to markdown` on a page whose losses
    cannot be classified made its Markdown copy publishable again, which is precisely what
    §10.2 forbids. The grade said the page must not be managed as Markdown and the marker
    overrode it.

    §10.2 allows either a re-grade or disabling the direction. This is the re-grade, and it
    needs no client: the storage is what gets graded and the storage is on disk whenever
    the caller holds an exact copy. So the copy has to be named -- a permission granted
    from a document nobody re-read is granted on the strength of whatever was true when it
    was last pulled.
    """

    from tests.unit.test_state_free_body_write import BodyClient

    client = BodyClient()
    managed = tmp_path / "page.md"
    pull_managed_suspending_the_write_policy(client, "123", managed, no_assets=True)

    with pytest.raises(ValidationError) as refused:
        set_authority("123", to="markdown", md_path=managed)

    assert refused.value.context["reason"] == "authority_grade_unavailable"


def test_authority_is_refused_for_a_page_whose_grade_forbids_markdown(tmp_path: Path) -> None:
    """The case the fixture measured: an unclassifiable page stays unpublishable."""

    from tests.unit.test_state_free_body_write import BodyClient

    unclassifiable = (
        '<table><tbody><tr><td style="background-color: rgb(255,0,0);"><p>cell</p></td></tr></tbody></table>'
    )
    client = BodyClient()
    client.storage = unclassifiable
    managed = tmp_path / "page.md"
    pull_managed_suspending_the_write_policy(client, "123", managed, no_assets=True)
    exact = tmp_path / "page.xhtml"
    pull_xhtml(client, "123", output_path=exact)

    with pytest.raises(ValidationError) as refused:
        set_authority("123", to="markdown", md_path=managed, xhtml_path=exact)

    assert refused.value.context["reason"] == "authority_refused_by_grade"
    assert refused.value.context["grade"] == "xhtml_required"
    # And the marker did not move despite the refusal.
    assert read_authority(managed) != "markdown" or read_authority(exact) != "markdown"


def test_taking_authority_away_from_markdown_still_needs_nothing(tmp_path: Path) -> None:
    """Asymmetric on purpose, and the asymmetry is the safety property.

    Removing permission cannot make anything publishable, so requiring evidence for it
    would only make the safe direction harder to reach -- and a safeguard that is awkward
    to apply is one that gets skipped.
    """

    from tests.unit.test_state_free_body_write import BodyClient

    client = BodyClient()
    managed = tmp_path / "page.md"
    pull_managed_suspending_the_write_policy(client, "123", managed, no_assets=True)

    result = set_authority("123", to="xhtml", md_path=managed)

    assert result["authority"] == "xhtml"
    assert read_authority(managed) == "xhtml"


def test_an_xhtml_only_transfer_cannot_grant_markdown_authority_to_a_sibling(tmp_path: Path) -> None:
    """R3-1. The guard has to cover what the sweep touches, not what the caller named.

    With only `--xhtml-file`, the grade check used to return early — nothing named, nothing to
    grant — and then the transfer swept the file's directory and wrote `markdown` authority onto
    the managed Markdown sibling beside it. `build_managed_preflight` reads that as `markdown`, so
    the `xhtml_is_authoritative` refusal stops applying and an `xhtml_required` document reaches
    the Markdown publish preflight through exactly the authority-only route §10.2 closes.

    Reproduced by the reviewer before it was accepted, and the shape is worth naming: a guard that
    inspects its argument while a later step reaches further is not a guard, it is a coincidence.
    """

    from tests.unit.test_state_free_body_write import BodyClient

    unclassifiable = (
        '<table><tbody><tr><td style="background-color: rgb(255,0,0);"><p>cell</p></td></tr></tbody></table>'
    )
    client = BodyClient()
    client.storage = unclassifiable
    managed = tmp_path / "page.md"
    pull_managed_suspending_the_write_policy(client, "123", managed, no_assets=True)
    exact = tmp_path / "page.xhtml"
    pull_xhtml(client, "123", output_path=exact)

    # No `md_path`. The Markdown copy is only reachable through the directory sweep.
    with pytest.raises(ValidationError) as refused:
        set_authority("123", to="markdown", xhtml_path=exact)

    assert refused.value.context["reason"] == "authority_refused_by_grade"
    assert read_authority(managed) != "markdown", "the sibling was granted authority by the sweep"


def test_taking_authority_to_xhtml_still_sweeps_without_evidence(tmp_path: Path) -> None:
    """The asymmetry survives the fix: removing permission needs nothing, in either direction."""

    from tests.unit.test_state_free_body_write import BodyClient

    client = BodyClient()
    managed = tmp_path / "page.md"
    pull_managed_suspending_the_write_policy(client, "123", managed, no_assets=True)
    exact = tmp_path / "page.xhtml"
    pull_xhtml(client, "123", output_path=exact)

    result = set_authority("123", to="xhtml", xhtml_path=exact)

    assert result["authority"] == "xhtml"
    assert read_authority(managed) == "xhtml"


def test_a_sidecar_whose_base_storage_does_not_match_its_own_digest_cannot_grant_authority(
    tmp_path: Path,
) -> None:
    """R3-4. The record carries the storage *and* its hash; trusting one without the other is not
    a check.

    The grade for `--to markdown` is computed from the XHTML sidecar's `base_storage`. The loader
    verifies that field is non-empty and that `page_id` is self-consistent, and never that
    `sha256(base_storage)` equals the `remote_storage_sha256` recorded beside it. So a sidecar that
    was copied from another page, left stale, or edited by hand can offer a Markdown-safe baseline
    while its own recorded hash describes an `xhtml_required` page — and the transfer then writes
    `markdown` authority onto the managed Markdown sibling.

    Reproduced by review R3 before it was accepted. Same shape as three earlier findings this
    release: a value read and trusted while the thing that would have caught it sat next to it.
    """

    import json

    from tests.unit.test_state_free_body_write import BodyClient

    unclassifiable = (
        '<table><tbody><tr><td style="background-color: rgb(255,0,0);"><p>cell</p></td></tr></tbody></table>'
    )
    client = BodyClient()
    client.storage = unclassifiable
    managed = tmp_path / "page.md"
    pull_managed_suspending_the_write_policy(client, "123", managed, no_assets=True)
    exact = tmp_path / "page.xhtml"
    pull_xhtml(client, "123", output_path=exact)

    # The tamper: a Markdown-safe body swapped into the record, leaving its digest untouched.
    side = sidecar_path(exact)
    payload = json.loads(side.read_text(encoding="utf-8"))
    payload["base_storage"] = "<p>alpha paragraph text</p><p>beta paragraph text</p>"
    side.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(ValidationError) as refused:
        set_authority("123", to="markdown", md_path=managed, xhtml_path=exact)

    assert refused.value.context["reason"] in {
        "authority_grade_unavailable",
        "sidecar_storage_digest_mismatch",
    }, refused.value.context
    assert read_authority(managed) != "markdown", "a tampered record granted Markdown authority"
