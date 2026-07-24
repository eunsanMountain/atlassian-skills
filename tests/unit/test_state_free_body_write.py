from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from atlassian_skills.confluence.pull_md import pull_md
from atlassian_skills.confluence.push_md import push_md
from atlassian_skills.core.errors import StaleError, ValidationError
from atlassian_skills.core.managed_manifest import parse_managed_document


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
    pull_md(client, "123", output_path=path, portable=True, no_assets=True)
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

    assert second["status"] == "reconciled"
    assert second["operation_id"] == first["operation_id"]
    assert client.puts == 1
    assert "atls:operation" not in path.read_text(encoding="utf-8")


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

    assert result["status"] == "readback_pending"
    [action] = result["next_actions"]
    assert action == {
        "id": "resume_operation",
        "requires_user_approval": False,
        "description_code": "RESUME_VERIFIED_OPERATION",
    }
    assert client.puts == 1
    assert "atls:operation" in path.read_text(encoding="utf-8")


def test_malformed_remote_readback_stays_fail_closed_without_conversion_crash(tmp_path: Path) -> None:
    client = BodyClient(fault="wrong_storage")
    path = tmp_path / "page.md"
    _checkout(client, path)
    _edit(path, "replace")

    first = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)
    assert first["status"] == "readback_pending"
    assert client.puts == 1

    client.storage = "<ac:structured-macro"
    second = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    assert second["status"] == "readback_pending"
    assert second["reason"] == "remote_candidate_readback_incomplete"
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
    pull_md(client, "123", output_path=path, portable=True, asset_dir=asset_dir)
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
