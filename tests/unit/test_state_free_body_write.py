from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import cfxmark
import pytest

from atlassian_skills.confluence.push_md import push_md
from atlassian_skills.core.errors import StaleError, ValidationError
from atlassian_skills.core.managed_manifest import parse_managed_document
from tests.unit.managed_seam import pull_managed_suspending_the_write_policy


class BodyClient:
    base_url = "https://example.com/confluence"

    def __init__(self, *, fault: str | None = None) -> None:
        self.storage = "<p>Base</p>"
        self.version = 7
        self.fault = fault
        self.gets = 0
        self.puts = 0
        self.on_readback: Callable[[], None] | None = None
        self.on_get: tuple[int, Callable[[], None]] | None = None
        self.update_kwargs: list[dict[str, object]] = []

    def get_page(self, page_id: str) -> SimpleNamespace:
        self.gets += 1
        if self.on_get is not None and self.gets == self.on_get[0]:
            self.on_get[1]()
        if self.fault == "stale_before_put" and self.gets == 2:
            self.storage = "<p>External</p>"
            self.version = 8
        if self.gets >= 3 and self.on_readback is not None:
            callback, self.on_readback = self.on_readback, None
            callback()
        if self.fault == "readback_unavailable_after_apply" and self.gets == 3:
            raise RuntimeError("readback unavailable after apply")
        return SimpleNamespace(
            id=page_id,
            title="Page",
            body_storage=self.storage,
            version=SimpleNamespace(number=self.version),
        )

    def update_page(
        self,
        *,
        page_id: str,
        title: str,
        body: str,
        version_number: int,
        **kwargs: object,
    ) -> dict[str, object]:
        self.puts += 1
        self.update_kwargs.append(kwargs)
        assert page_id == "123"
        assert title == "Page"
        assert version_number == self.version + 1
        if self.fault == "loss_before_apply":
            raise RuntimeError("response lost before apply")
        self.version = version_number
        if self.fault == "wrong_storage":
            self.storage = "<p>Wrong</p>"
        elif self.fault == "readback_unavailable_after_apply":
            self.storage = body + "\n"
        else:
            self.storage = body
        if self.fault == "loss_after_apply":
            raise RuntimeError("response lost after apply")
        return {"id": page_id, "version": {"number": version_number}}


def _checkout(client: BodyClient, path: Path) -> None:
    pull_managed_suspending_the_write_policy(client, "123", path, no_assets=True)
    client.gets = 0


def _edit(path: Path, mode: str) -> None:
    text = path.read_text(encoding="utf-8")
    if mode == "append":
        path.write_text(text + "\nAdded\n", encoding="utf-8")
    else:
        path.write_text(text.replace("Base", "Changed"), encoding="utf-8")


@pytest.mark.parametrize(
    ("mode", "proof", "reason_prefix"),
    [
        ("append", "exact_remote_prefix_append", "atls exact EOF append "),
        ("replace", "full_migration", "atls markdown migration "),
    ],
)
@pytest.mark.parametrize("user_reason", [None, "Caller supplied reason"])
def test_body_publish_writes_journal_puts_reads_back_and_finalizes(
    tmp_path: Path,
    mode: str,
    proof: str,
    reason_prefix: str,
    user_reason: str | None,
) -> None:
    client = BodyClient()
    path = tmp_path / "page.md"
    _checkout(client, path)
    _edit(path, mode)

    result = push_md(
        client,
        "123",
        path.read_text(encoding="utf-8"),
        managed_path=path,
        reason=user_reason,
    )

    managed = path.read_text(encoding="utf-8")
    document = parse_managed_document(managed, verify_content=True)
    assert result["status"] == "reconciled"
    assert result["proof_mode"] == proof
    assert result["version"] == 8
    assert client.puts == 1
    version_reason = client.update_kwargs[0]["reason"]
    assert isinstance(version_reason, str)
    assert version_reason.startswith(reason_prefix)
    if user_reason is not None:
        assert version_reason.endswith(f"; {user_reason}")
    else:
        assert not version_reason.endswith("; ")
    assert "atls:operation" not in managed
    assert document.manifest.remote_version == 8


def test_body_response_loss_is_adopted_by_readback_without_duplicate_put(tmp_path: Path) -> None:
    client = BodyClient(fault="loss_after_apply")
    path = tmp_path / "page.md"
    _checkout(client, path)
    _edit(path, "replace")

    result = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    assert result["status"] == "reconciled"
    assert result["adopted_response_loss"] is True
    assert client.puts == 1
    assert "atls:operation" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "edited_markdown",
    [
        "_ital_",
        "__bold__",
        "* one",
        "1) one",
        "Title\n=====",
        "~~~\ncode\n~~~",
        "<https://a.example>",
        "# T #",
    ],
)
def test_full_migration_recovery_accepts_noncanonical_markdown_without_duplicate_put(
    tmp_path: Path,
    edited_markdown: str,
) -> None:
    client = BodyClient(fault="readback_unavailable_after_apply")
    path = tmp_path / "page.md"
    _checkout(client, path)
    original = path.read_text(encoding="utf-8")
    path.write_text(original + f"\n{edited_markdown}\n", encoding="utf-8")

    first = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    assert first["status"] == "readback_pending"
    assert first["proof_mode"] == "full_migration"
    assert client.puts == 1
    assert "atls:operation" in path.read_text(encoding="utf-8")

    second = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    # `published_normalized`, and every parametrisation of this test is a fixed point
    # for it: the author wrote a spelling Markdown accepts and the round trip returns
    # the canonical one, so the file on disk stops being the bytes that were submitted.
    # §9.10 requires that to be reported rather than absorbed into a plain success --
    # otherwise the author's next diff shows edits nobody made.
    assert second["status"] == "published_normalized"
    assert second["reason"] == "remote_normalized_the_submitted_markdown"
    assert second["local_rewrite"]["submitted_md_sha256"] != second["local_rewrite"]["stored_md_sha256"]
    assert second["local_rewrite"]["path"] == str(path)
    assert second["operation_id"] == first["operation_id"]
    # The publish is still one PUT, which is what this test is named for.
    assert client.puts == 1
    assert "atls:operation" not in path.read_text(encoding="utf-8")

    # And the baseline the manifest now carries is the file that is actually on disk,
    # so the next compare sees no local edit. Binding the submitted text was what made
    # a compare report a change the author had not made.
    from atlassian_skills.core.managed_manifest import canonical_content_sha256, strip_managed_manifest

    body, manifest = strip_managed_manifest(path.read_text(encoding="utf-8"))
    assert manifest.base_md == canonical_content_sha256(body)
    assert manifest.base_md == second["local_rewrite"]["stored_md_sha256"]


def test_transition_marker_to_current_stage_is_idempotent(tmp_path: Path) -> None:
    # A second read-back failure while recovering an operation already marked
    # body_applied_readback_pending re-emits the current stage.  That must be an
    # idempotent no-op, not an uncaught ManagedOperationError escaping as a
    # traceback.
    from atlassian_skills.confluence.body_write import _read_exact, _transition_marker
    from atlassian_skills.confluence.managed_operation import parse_managed_operation
    from atlassian_skills.core.directory_capability import DirectoryCapability

    client = BodyClient(fault="readback_unavailable_after_apply")
    path = tmp_path / "page.md"
    _checkout(client, path)
    original = path.read_text(encoding="utf-8")
    path.write_text(original + "\nAdded line.\n", encoding="utf-8")

    first = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)
    assert first["status"] == "readback_pending"

    with DirectoryCapability.acquire(path.parent) as capability:
        text = _read_exact(capability, path)
        operation = parse_managed_operation(text)
        assert operation is not None
        assert operation.stage == "body_applied_readback_pending"

        result = _transition_marker(capability, path, text, operation, operation.stage)

    assert result is not None
    same_text, same_operation = result
    assert same_text == text
    assert same_operation.stage == "body_applied_readback_pending"
    assert path.read_text(encoding="utf-8") == text


def test_unknown_unapplied_put_stays_journaled_then_retry_uses_same_operation(tmp_path: Path) -> None:
    client = BodyClient(fault="loss_before_apply")
    path = tmp_path / "page.md"
    _checkout(client, path)
    _edit(path, "replace")

    first = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)
    operation_id = first["operation_id"]
    assert first["status"] == "assets_applied_body_pending"
    assert "atls:operation" in path.read_text(encoding="utf-8")
    assert client.puts == 1

    client.fault = None
    second = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    assert second["status"] == "reconciled"
    assert second["operation_id"] == operation_id
    assert client.puts == 2
    assert client.update_kwargs[0]["reason"] == client.update_kwargs[1]["reason"]
    assert "atls:operation" not in path.read_text(encoding="utf-8")


def test_first_mutation_revalidation_stale_performs_zero_put_and_cleans_unstarted_marker(tmp_path: Path) -> None:
    client = BodyClient(fault="stale_before_put")
    path = tmp_path / "page.md"
    _checkout(client, path)
    _edit(path, "replace")

    with pytest.raises(StaleError):
        push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    assert client.puts == 0
    assert "atls:operation" not in path.read_text(encoding="utf-8")


def test_local_markdown_changed_after_preflight_is_blocked_before_journal_or_put(tmp_path: Path) -> None:
    client = BodyClient()
    path = tmp_path / "page.md"
    _checkout(client, path)
    _edit(path, "replace")
    client.on_get = (1, lambda: path.write_text(path.read_text(encoding="utf-8") + "local\n", encoding="utf-8"))

    with pytest.raises(ValidationError) as exc_info:
        push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    assert exc_info.value.context["reason"] == "local_changed_after_preflight"
    assert client.puts == 0
    assert "atls:operation" not in path.read_text(encoding="utf-8")


def test_mismatched_expected_readback_keeps_journal_and_performs_no_retry_put(tmp_path: Path) -> None:
    client = BodyClient(fault="wrong_storage")
    path = tmp_path / "page.md"
    _checkout(client, path)
    _edit(path, "replace")

    result = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    # `manual_recovery`, and the advice moved with it. A readback that disagreed used
    # to report `readback_pending` and offer `resume_operation` -- telling the caller to
    # carry on with an operation whose page is holding a body nobody in it wrote. The
    # action below is derived from the journal stage, so naming the outcome correctly
    # changed the advice without a second edit, which is the sign the name was the
    # defect rather than a label on one.
    assert result["status"] == "manual_recovery"
    assert result["reason"] == "remote_body_did_not_match_candidate"
    [action] = result["next_actions"]
    assert action == {
        "id": "inspect_operation",
        "requires_user_approval": False,
        "description_code": "INSPECT_OPERATION_RECOVERY",
    }
    assert client.puts == 1
    assert "atls:operation" in path.read_text(encoding="utf-8")


def test_malformed_remote_readback_stays_fail_closed_without_conversion_crash(tmp_path: Path) -> None:
    """The retry converts whatever the page is holding, and it may be unparseable.

    The setup used to reach this through `fault="wrong_storage"`, which left the
    operation at `readback_pending` because a readback that *disagreed* was reported
    as one that had not happened. Once that was corrected the first push ends at
    `manual_recovery`, the retry short-circuits there, and the malformed body is never
    converted -- so this test would have kept its name and stopped exercising it.

    A readback that genuinely did not happen is the honest way in: `readback_pending`
    is true, retrying is right, and the retry is what meets the malformed storage.
    """

    client = BodyClient(fault="readback_unavailable_after_apply")
    path = tmp_path / "page.md"
    _checkout(client, path)
    _edit(path, "replace")

    first = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)
    assert first["status"] == "readback_pending"
    assert first["reason"] == "remote_readback_unavailable"
    assert client.puts == 1

    client.fault = None
    client.storage = "<ac:structured-macro"
    second = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    assert second["status"] == "readback_pending"
    assert second["reason"] == "remote_candidate_readback_incomplete"
    assert client.puts == 1
    assert "atls:operation" in path.read_text(encoding="utf-8")


def test_a_page_already_in_manual_recovery_is_not_retried_by_another_push(tmp_path: Path) -> None:
    """The path the test above used to take, pinned for what it actually is now.

    Once a push has found a body that is neither the candidate nor the source, the
    operation needs a person. A second push must not quietly re-attempt, and must not
    convert or adopt whatever is on the page in the meantime -- so it reports the
    stage it is stuck at and touches nothing.
    """

    client = BodyClient(fault="wrong_storage")
    path = tmp_path / "page.md"
    _checkout(client, path)
    _edit(path, "replace")

    first = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)
    assert first["status"] == "manual_recovery"

    client.storage = "<ac:structured-macro"
    second = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    assert second["status"] == "manual_recovery"
    assert second["reason"] == "operation_requires_manual_recovery"
    assert client.puts == 1
    assert "atls:operation" in path.read_text(encoding="utf-8")


def test_local_change_before_finalize_is_preserved_with_structured_conflict(tmp_path: Path) -> None:
    client = BodyClient()
    path = tmp_path / "page.md"
    _checkout(client, path)
    _edit(path, "replace")
    client.on_readback = lambda: path.write_text(path.read_text(encoding="utf-8") + "local\n", encoding="utf-8")

    result = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    assert result["status"] == "local_finalize_conflict"
    assert client.puts == 1
    assert path.read_text(encoding="utf-8").endswith("local\n")


def test_same_target_asset_ancestor_symlink_swap_is_blocked_before_remote_mutation(tmp_path: Path) -> None:
    from atlassian_skills.confluence.body_write import publish_managed_body
    from atlassian_skills.confluence.migration_preflight import build_managed_preflight

    client = BodyClient()
    client.storage = (
        '<p>Base</p><p><ac:image xmlns:ac="http://atlassian.com/content" '
        'xmlns:ri="http://atlassian.com/resource/identifier"><ri:attachment '
        'ri:filename="diagram.png"/></ac:image></p>'
    )
    client.list_attachments = lambda _page_id: [
        SimpleNamespace(
            id="att-1",
            title="diagram.png",
            version=SimpleNamespace(number=4),
            media_type="image/png",
            links=SimpleNamespace(download="/download/att-1"),
        )
    ]
    client.fetch_attachment_bytes = lambda _attachment_id, _download_link: b"old"
    client.uploads = 0

    def upload(*_args: object, **_kwargs: object) -> dict[str, object]:
        client.uploads += 1
        return {}

    client._upload_attachment_raw = upload
    path = tmp_path / "page.md"
    asset_dir = tmp_path / "page.assets"
    pull_managed_suspending_the_write_policy(client, "123", path, asset_dir=asset_dir)
    [asset] = asset_dir.iterdir()
    asset.write_bytes(b"new")
    preflight = build_managed_preflight(client, "123", path)
    moved = tmp_path / "moved-assets"
    asset_dir.rename(moved)
    asset_dir.symlink_to(moved, target_is_directory=True)

    with pytest.raises(ValidationError) as exc_info:
        publish_managed_body(client, preflight, path)

    assert exc_info.value.context["reason"] == "asset_changed_after_preflight"
    assert client.puts == 0
    assert client.uploads == 0
    assert "atls:operation" not in path.read_text(encoding="utf-8")


def test_mutation_boundary_requires_the_exact_migration_consent_fingerprint(tmp_path: Path) -> None:
    from atlassian_skills.confluence.body_write import publish_managed_body
    from atlassian_skills.confluence.migration_preflight import build_managed_preflight

    client = BodyClient()
    client.storage = '<p><ac:emoticon ac:name="smile"/></p><p>Base</p>'
    path = tmp_path / "page.md"
    _checkout(client, path)
    _edit(path, "replace")
    preflight = build_managed_preflight(client, "123", path)

    assert preflight.consent_required is True
    assert preflight.migration_fingerprint is not None
    for accepted in (None, "mig_sha256:" + "0" * 64):
        with pytest.raises(ValidationError) as exc_info:
            publish_managed_body(client, preflight, path, accept_migration=accepted)
        assert exc_info.value.context["reason"] == "migration_consent_required"
        assert client.puts == 0
        assert "atls:operation" not in path.read_text(encoding="utf-8")

    result = publish_managed_body(
        client,
        preflight,
        path,
        accept_migration=preflight.migration_fingerprint,
    )

    assert result["status"] == "reconciled"
    assert client.puts == 1


def test_pending_full_migration_cannot_bypass_consent_through_recovery(tmp_path: Path) -> None:
    from atlassian_skills.confluence.managed_operation import insert_managed_operation, operation_for_preflight
    from atlassian_skills.confluence.migration_preflight import build_managed_preflight

    client = BodyClient()
    client.storage = '<p><ac:emoticon ac:name="smile"/></p><p>Base</p>'
    path = tmp_path / "page.md"
    _checkout(client, path)
    _edit(path, "replace")
    preflight = build_managed_preflight(client, "123", path)
    assert preflight.consent_required is True
    assert preflight.migration_fingerprint is not None

    marked = insert_managed_operation(path.read_text(encoding="utf-8"), operation_for_preflight(preflight))
    path.write_text(marked, encoding="utf-8")

    for accepted in (None, "mig_sha256:" + "0" * 64):
        with pytest.raises(ValidationError) as exc_info:
            push_md(
                client,
                "123",
                path.read_text(encoding="utf-8"),
                managed_path=path,
                accept_migration=accepted,
            )
        assert exc_info.value.context["reason"] == "migration_consent_required"
        assert client.puts == 0
        assert "atls:operation" in path.read_text(encoding="utf-8")

    result = push_md(
        client,
        "123",
        path.read_text(encoding="utf-8"),
        managed_path=path,
        accept_migration=preflight.migration_fingerprint,
    )

    assert result["status"] == "reconciled"
    assert client.puts == 1
    assert "atls:operation" not in path.read_text(encoding="utf-8")


def test_landed_consent_bound_write_reconciles_read_only_without_reconsent(tmp_path: Path) -> None:
    from atlassian_skills.confluence.migration_preflight import build_managed_preflight

    client = BodyClient(fault="readback_unavailable_after_apply")
    client.storage = '<p><ac:emoticon ac:name="smile"/></p><p>Base</p>'
    path = tmp_path / "page.md"
    _checkout(client, path)
    _edit(path, "replace")
    preflight = build_managed_preflight(client, "123", path)
    assert preflight.migration_fingerprint is not None
    client.gets = 0

    first = push_md(
        client,
        "123",
        path.read_text(encoding="utf-8"),
        managed_path=path,
        accept_migration=preflight.migration_fingerprint,
    )
    assert first["status"] == "readback_pending"
    assert client.puts == 1

    second = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    assert second["status"] == "reconciled"
    assert client.puts == 1
    assert "atls:operation" not in path.read_text(encoding="utf-8")


def test_managed_parent_symlink_swap_after_journal_is_blocked_before_put(tmp_path: Path) -> None:
    client = BodyClient()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    current = tmp_path / "current"
    current.symlink_to(first, target_is_directory=True)
    path = current / "page.md"
    _checkout(client, path)
    _edit(path, "replace")

    def swap_managed_parent() -> None:
        marked = (first / "page.md").read_bytes()
        (second / "page.md").write_bytes(marked)
        current.unlink()
        current.symlink_to(second, target_is_directory=True)

    client.on_get = (2, swap_managed_parent)

    with pytest.raises(ValidationError) as exc_info:
        push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    assert exc_info.value.context["reason"] == "managed_directory_changed"
    assert client.puts == 0
    assert "atls:operation" in (first / "page.md").read_text(encoding="utf-8")
    assert "atls:operation" in (second / "page.md").read_text(encoding="utf-8")


def test_managed_parent_symlink_swap_after_readback_is_blocked_before_finalize(tmp_path: Path) -> None:
    client = BodyClient()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    current = tmp_path / "current"
    current.symlink_to(first, target_is_directory=True)
    path = current / "page.md"
    _checkout(client, path)
    _edit(path, "replace")

    def swap_managed_parent() -> None:
        marked = (first / "page.md").read_bytes()
        (second / "page.md").write_bytes(marked)
        current.unlink()
        current.symlink_to(second, target_is_directory=True)

    client.on_readback = swap_managed_parent

    with pytest.raises(ValidationError) as exc_info:
        push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    assert exc_info.value.context["reason"] == "managed_directory_changed"
    assert client.puts == 1
    assert "atls:operation" in (first / "page.md").read_text(encoding="utf-8")
    assert "atls:operation" in (second / "page.md").read_text(encoding="utf-8")


def test_a_normalized_asset_bearing_publish_keeps_its_local_asset_paths(tmp_path: Path) -> None:
    """Blocker 12 / R3-3: the projection is rewritten, not skipped.

    A portable managed file points its image links at local asset paths; a projection of remote
    storage names the attachments the way the server does. They are supposed to differ, so an
    earlier attempt wrote the projection verbatim and turned every local link back into a remote
    name — measured, which is why asset-bearing documents were then skipped entirely and `base_md`
    kept binding the submitted Markdown.

    Skipping them left the defect it was avoiding: a page the server normalises gets a manifest
    describing text the server is not holding, and the next compare reports an edit nobody made.

    So the rewrite the pull applies is applied here too, and the two things that must both hold are
    asserted together: the local asset path survives, and `base_md` matches the file on disk.
    """

    from atlassian_skills.core.managed_manifest import canonical_content_sha256, strip_managed_manifest

    # An actually asset-bearing document. The first version of this test used `_checkout`, whose
    # page has no attachments — so `records` was empty, both branches of the fix behaved
    # identically, and the mutation that restored the defect passed. A fixture that cannot tell
    # the fix from the bug is not a guard, which is the trap this release has already met once.
    # The fault is set after the pull: an asset-bearing pull makes more calls than a plain one, and
    # arming it at construction failed during the pull instead of during the publish.
    client = BodyClient()
    client.storage = (
        '<p>Base</p><p><ac:image xmlns:ac="http://atlassian.com/content" '
        'xmlns:ri="http://atlassian.com/resource/identifier"><ri:attachment '
        'ri:filename="diagram.png"/></ac:image></p>'
    )
    client.list_attachments = lambda _page_id: [
        SimpleNamespace(
            id="att-1",
            title="diagram.png",
            version=SimpleNamespace(number=4),
            media_type="image/png",
            links=SimpleNamespace(download="/download/att-1"),
        )
    ]
    client.fetch_attachment_bytes = lambda _attachment_id, _download_link: b"old"
    path = tmp_path / "page.md"
    asset_dir = tmp_path / "page.assets"
    pull_managed_suspending_the_write_policy(client, "123", path, asset_dir=asset_dir)
    original = path.read_text(encoding="utf-8")
    assert "page.assets/" in original, "this fixture is not asset-bearing"
    # A non-canonical spelling, so the round trip normalises and the projection differs from what
    # was submitted — which is the branch this test is about. No injected fault: an asset-bearing
    # pull makes more calls than a plain one and the readback fault escaped from one of them, and
    # the fault was never the subject here anyway.
    path.write_text(original + "\n_ital_\n", encoding="utf-8")

    receipt = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    # `published_normalized` specifically, not "either outcome". Accepting both is what let the
    # mutation through the first time: under the old behaviour -- skip asset-bearing documents and
    # bind the submitted Markdown -- the file keeps `_ital_`, `base_md` hashes `_ital_`, and the two
    # agree with each other while disagreeing with the server. Self-consistent and wrong, and an
    # assertion that accepts both cannot see it.
    assert receipt["status"] == "published_normalized", receipt
    assert receipt["local_rewrite"]["submitted_md_sha256"] != receipt["local_rewrite"]["stored_md_sha256"]
    body, manifest = strip_managed_manifest(path.read_text(encoding="utf-8"))

    # Whatever branch it took, the baseline describes the file that is on disk. That is the
    # invariant blocker 12 broke for asset-bearing documents, and it is the one worth pinning:
    # a manifest that disagrees with its own file makes the next compare report a phantom edit.
    assert manifest.base_md == canonical_content_sha256(body)
    # And the local asset path survived rather than being replaced by the remote name the
    # projection carries. That substitution is the concrete harm: the next reader opens a link
    # that resolves on the server and not in this copy.
    assert "page.assets/" in body, "the projection's remote attachment name replaced the local path"


# --------------------------------------------------------------------------
# The presentation change reaches the receipt, and says "unknown" when it is
# --------------------------------------------------------------------------


def test_the_blank_marker_preserves_storage_without_consent(tmp_path: Path) -> None:
    """The marker eliminates the old first-publish presentation change."""

    client = BodyClient()
    client.storage = "<p>Base</p><p /><p>Tail</p>"
    path = tmp_path / "page.md"
    _checkout(client, path)
    _edit(path, "replace")

    dry = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path, dry_run=True)
    assert dry["candidate_loss"]["first_publish_changes_presentation"] is False
    assert dry["candidate_loss"]["affected_occurrences"] == 0

    result = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)
    assert result["status"] == "reconciled"
    assert result["first_publish_changes_presentation"] is False
    assert result["affected_occurrences"] == 0
    assert client.storage == "<p>Changed</p><p/><p>Tail</p>"


def test_a_publish_that_changes_no_presentation_says_false_on_the_receipt(tmp_path: Path) -> None:
    client = BodyClient()
    path = tmp_path / "page.md"
    _checkout(client, path)
    _edit(path, "replace")

    result = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)
    assert result["first_publish_changes_presentation"] is False
    assert result["affected_occurrences"] == 0


# --------------------------------------------------------------------------
# The consent gate follows the classification, one shape at a time
# --------------------------------------------------------------------------


def test_blank_markers_preserve_body_and_list_paragraphs_exactly(tmp_path: Path) -> None:
    """The marker is structural and reconstructs each source location exactly."""

    client = BodyClient()
    client.storage = "<p>Base</p><p /><p>Tail</p><ul><li><p>x</p><p /><p>y</p></li></ul>"
    path = tmp_path / "page.md"
    _checkout(client, path)
    _edit(path, "replace")

    result = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    assert result["first_publish_changes_presentation"] is False
    assert result["affected_occurrences"] == 0
    assert client.storage == "<p>Changed</p><p/><p>Tail</p><ul><li><p>x</p><p/><p>y</p></li></ul>"


# --------------------------------------------------------------------------
# The empty paragraph is a fixed point, from the pull's first output
# --------------------------------------------------------------------------
#
# The trap this guards is a first push that succeeds and leaves the author's file
# different from what they submitted. It costs nothing and it is corrosive: the next
# `git diff` shows a change nobody made, in a document the author is being asked to
# trust as canonical, and after the second time they stop reading the diffs.
#
# The way out is for the pull to emit the form the push will store, so the round trip
# has a fixed point rather than a first-run adjustment.


def test_the_pull_writes_the_form_the_push_will_store(tmp_path: Path) -> None:
    client = BodyClient()
    client.storage = "<p>Base</p><p /><p>Tail</p>"
    path = tmp_path / "page.md"
    _checkout(client, path)

    document = parse_managed_document(path.read_text(encoding="utf-8"), verify_content=True)
    assert cfxmark.strip_header_notice(document.content) == "Base\n\n<!-- cfxmark:blank -->\n\nTail\n"


def test_the_first_push_does_not_rewrite_the_authors_file(tmp_path: Path) -> None:
    # No empty paragraph: this test is about whether the author's file is rewritten, and since
    # R4-pre an empty paragraph stops the publish for consent -- which would test the gate.
    client = BodyClient()
    client.storage = "<p>Base</p><p>Tail</p>"
    path = tmp_path / "page.md"
    _checkout(client, path)
    _edit(path, "replace")
    submitted = path.read_text(encoding="utf-8")

    result = push_md(client, "123", submitted, managed_path=path)

    assert result["status"] == "reconciled"
    # Not `published_normalized`: that receipt exists for a server normalisation the
    # file has to be brought into line with, and there is nothing to bring into line
    # here. Getting this wrong is silent -- the publish still succeeds.
    body_before = submitted.split("-->\n")[-1]
    body_after = path.read_text(encoding="utf-8").split("-->\n")[-1]
    assert body_after == body_before
    # And the remote holds the canonical form, so the two now agree.
    assert client.storage == "<p>Changed</p><p>Tail</p>"


def test_pushing_twice_is_a_fixed_point_not_a_second_edit(tmp_path: Path) -> None:
    """The second push must find nothing to do.

    "The file was not rewritten" is only half the claim. If the round trip had no
    fixed point, the file and the remote would each be self-consistent and disagree
    with each other, and every push would send a PUT for a change the author never
    made.
    """

    # No empty paragraph: this test is about whether the author's file is rewritten, and since
    # R4-pre an empty paragraph stops the publish for consent -- which would test the gate.
    client = BodyClient()
    client.storage = "<p>Base</p><p>Tail</p>"
    path = tmp_path / "page.md"
    _checkout(client, path)
    _edit(path, "replace")

    push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)
    after_first = path.read_text(encoding="utf-8")
    second = push_md(client, "123", after_first, managed_path=path)

    assert second["status"] == "no_change"
    assert second["put_count"] == 0
    assert client.puts == 1
    assert path.read_text(encoding="utf-8") == after_first


def test_an_unrelated_edit_preserves_the_blank_paragraph_without_consent(tmp_path: Path) -> None:
    """A full managed edit keeps the empty-paragraph shape instead of adding a break."""

    client = BodyClient()
    client.storage = "<p>Base</p><p /><p>Tail</p>"
    path = tmp_path / "page.md"
    _checkout(client, path)
    _edit(path, "replace")

    result = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    assert result["first_publish_changes_presentation"] is False
    assert client.storage == "<p>Changed</p><p/><p>Tail</p>"


def test_an_append_changes_no_presentation_and_needs_no_consent(tmp_path: Path) -> None:
    """The other half, and why this is not simply "ask about everything".

    The append proof reuses the remote prefix byte for byte, so an untouched `<p/>` is still
    `<p/>` afterwards and there is nothing to approve. Asking there would be the
    over-blocking this release exists to remove.
    """

    client = BodyClient()
    client.storage = "<p>Base</p><p /><p>Tail</p>"
    path = tmp_path / "page.md"
    _checkout(client, path)
    _edit(path, "append")

    result = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    assert result["status"] == "reconciled"
    assert result["first_publish_changes_presentation"] is False
    assert client.storage == "<p>Base</p><p /><p>Tail</p><p>Added</p>"
