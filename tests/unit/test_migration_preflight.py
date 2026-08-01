from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import cfxmark
import pytest

from atlassian_skills.confluence.migration_preflight import build_managed_preflight
from atlassian_skills.confluence.pull_md import pull_md
from atlassian_skills.confluence.push_md import push_md
from atlassian_skills.core.errors import MigrationConsentRequiredError, StaleError, ValidationError
from tests.unit.managed_seam import pull_managed_suspending_the_write_policy


class FakeClient:
    base_url = "https://example.com/confluence"

    def __init__(self, storage: str = "<p>Base</p>", *, version: int = 7) -> None:
        self.storage = storage
        self.version = version
        self.puts = 0
        self.get_calls = 0

    def get_page(self, page_id: str) -> SimpleNamespace:
        self.get_calls += 1
        return SimpleNamespace(
            id=page_id,
            title="Page",
            body_storage=self.storage,
            version=SimpleNamespace(number=self.version),
        )


def _pull(client: FakeClient, path: Path, *, passthrough: list[str] | None = None) -> None:
    pull_managed_suspending_the_write_policy(client, "123", path, no_assets=True, passthrough_prefixes=passthrough)


def test_preflight_selects_no_change_before_other_proof_modes(tmp_path: Path) -> None:
    client = FakeClient()
    path = tmp_path / "page.md"
    _pull(client, path)

    proof = build_managed_preflight(client, "123", path)

    assert proof.proof_mode == "no_change"
    assert proof.status == "no_change"
    assert proof.body_dirty is False
    assert proof.asset_dirty is False
    assert proof.candidate_storage == client.storage


def test_managed_no_change_receipt_carries_put_count_zero(tmp_path: Path) -> None:
    # The managed no_change receipt is uniform with the stateless one (which already
    # returns put_count 0), so an external receipt consumer never has to special-case
    # managed vs stateless when deciding a body update succeeded without a write.
    client = FakeClient()
    path = tmp_path / "page.md"
    _pull(client, path)

    result = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    assert result["status"] == "no_change"
    assert result["put_count"] == 0
    assert result["remote_version"] == 7
    assert client.puts == 0


def test_preflight_exact_append_preserves_remote_storage_as_byte_prefix(tmp_path: Path) -> None:
    client = FakeClient("<p>Rich <strong>base</strong></p>")
    path = tmp_path / "page.md"
    _pull(client, path)
    path.write_text(path.read_text(encoding="utf-8") + "\n## Added\n\nSafe paragraph.\n", encoding="utf-8")

    proof = build_managed_preflight(client, "123", path)

    assert proof.proof_mode == "exact_remote_prefix_append"
    assert proof.consent_required is False
    assert proof.append_sha256 is not None
    assert proof.candidate_storage.startswith(client.storage)
    assert proof.candidate_storage[: len(client.storage)] == client.storage
    assert proof.deferred_migrations == ()


def test_in_place_edit_uses_source_bound_full_migration_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient('<p><ac:emoticon ac:name="smile"/></p><p>before</p>')
    path = tmp_path / "page.md"
    _pull(client, path)
    path.write_text(path.read_text(encoding="utf-8").replace("before", "changed"), encoding="utf-8")

    proof = build_managed_preflight(client, "123", path)

    assert proof.proof_mode == "full_migration"
    assert proof.status == "migration_consent_required"
    assert proof.migration_fingerprint is not None
    assert proof.migration_report_sha256.startswith("sha256:")
    assert proof.ownership["unclassified"] == []
    assert proof.ownership["multiple_owners"] == []
    assert proof.ownership["overlap"] == []
    assert proof.ownership["accepted_migration_occurrence_ids"]
    assert proof.candidate is not None
    assert proof.candidate.source_storage_sha256 == proof.remote_storage_sha256.removeprefix("sha256:")

    def repeated_measurement(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("serialising one preflight must not remeasure its fresh remote")

    monkeypatch.setattr("atlassian_skills.confluence.migration_preflight.compatibility_payload", repeated_measurement)
    monkeypatch.setattr("atlassian_skills.confluence.migration_preflight.candidate_loss", repeated_measurement)
    payload = proof.to_dict()
    assert payload["compatibility"] == proof.compatibility
    assert payload["candidate_loss"] == proof.candidate_loss_payload


def test_fingerprint_changes_when_local_candidate_changes(tmp_path: Path) -> None:
    client = FakeClient('<p><ac:emoticon ac:name="smile"/></p><p>before</p>')
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    _pull(client, first)
    second.write_bytes(first.read_bytes())
    first.write_text(first.read_text(encoding="utf-8").replace("before", "first"), encoding="utf-8")
    second.write_text(second.read_text(encoding="utf-8").replace("before", "second"), encoding="utf-8")

    first_proof = build_managed_preflight(client, "123", first)
    second_proof = build_managed_preflight(client, "123", second)

    assert first_proof.migration_fingerprint != second_proof.migration_fingerprint
    assert first_proof.migration_report_sha256 == second_proof.migration_report_sha256


def test_remote_version_or_storage_drift_is_stale_before_candidate(tmp_path: Path) -> None:
    client = FakeClient()
    path = tmp_path / "page.md"
    _pull(client, path)
    client.version += 1
    client.storage = "<p>Remote changed</p>"

    with pytest.raises(StaleError) as error:
        build_managed_preflight(client, "123", path)

    assert error.value.context["reason"] == "remote_stale"


def test_old_converter_manifest_requires_refresh_before_remote_read(tmp_path: Path) -> None:
    """A managed artifact from another converter cannot silently cross the identity boundary."""

    client = FakeClient()
    path = tmp_path / "legacy-converter.md"
    _pull(client, path)
    current = path.read_text(encoding="utf-8")
    # Rewrite whatever converter the pull stamped, so this keeps testing the
    # boundary rather than a version literal that goes stale every release.
    legacy = current.replace(f"converter=cfxmark/{cfxmark.__version__}", "converter=cfxmark/0.0.0", 1)
    assert legacy != current, "the pulled manifest did not carry the installed converter"
    path.write_text(legacy, encoding="utf-8")
    client.get_calls = 0

    with pytest.raises(ValidationError) as error:
        build_managed_preflight(client, "123", path)

    assert error.value.context == {"reason": "managed_converter_mismatch"}
    assert client.get_calls == 0
    assert client.puts == 0
    # A converter upgrade invalidates every managed file at once, so the error has to
    # carry the way out — plain output prints message + hint and nothing else.
    assert error.value.hint and "pull-md" in error.value.hint


def test_supplied_passthrough_must_exactly_match_manifest(tmp_path: Path) -> None:
    client = FakeClient()
    path = tmp_path / "page.md"
    _pull(client, path, passthrough=["ac:"])

    with pytest.raises(ValidationError) as error:
        build_managed_preflight(client, "123", path, passthrough_prefixes=("other:",))

    assert error.value.context["reason"] == "passthrough_mismatch"


def test_legacy_binding_marker_push_reports_repull_required(tmp_path: Path) -> None:
    # A 0.2.x `atls:binding v1` file must fail the managed push preflight with
    # the same `legacy_manifest_repull_required` reason that diff-local and
    # validate-local use, plus re-pull guidance, and without any remote write.
    client = FakeClient()
    path = tmp_path / "legacy.md"
    path.write_text('<!-- atls:binding {"v":1,"id":"bnd_dead"} -->\n\n# Draft\n', encoding="utf-8")

    with pytest.raises(ValidationError) as error:
        build_managed_preflight(client, "123", path)

    assert error.value.context["reason"] == "legacy_manifest_repull_required"
    assert "pull-md" in str(error.value)
    assert client.puts == 0


def test_dry_run_and_missing_consent_share_exact_report_and_write_nothing(tmp_path: Path) -> None:
    client = FakeClient('<p><ac:emoticon ac:name="smile"/></p><p>before</p>')
    path = tmp_path / "page.md"
    _pull(client, path)
    path.write_text(path.read_text(encoding="utf-8").replace("before", "changed"), encoding="utf-8")

    dry_run = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path, dry_run=True)
    with pytest.raises(MigrationConsentRequiredError) as exc_info:
        push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)

    missing = exc_info.value.context
    assert dry_run["migration_fingerprint"] == missing["migration_fingerprint"]
    assert dry_run["migration_report_sha256"] == missing["migration_report_sha256"]
    assert missing["status"] == "migration_consent_required"
    [action] = missing["next_actions"]
    assert action["id"] == "retry_with_consent"
    assert action["requires_user_approval"] is True
    assert action["description_code"] == "REVIEW_MIGRATION_AND_RETRY"
    assert action["argv"][-1] == missing["migration_fingerprint"]
    assert client.puts == 0


def test_synthetic_fifty_page_exact_append_dry_run_is_no_write_and_deterministic(tmp_path: Path) -> None:
    for ordinal in range(50):
        page_id = str(10_000 + ordinal)
        client = FakeClient(f"<p>Rich <strong>base {ordinal}</strong></p>", version=ordinal + 1)
        path = tmp_path / f"page-{ordinal}.md"
        pull_md(client, page_id, output_path=path, portable=True, no_assets=True)
        path.write_text(
            path.read_text(encoding="utf-8") + f"\nSynthetic append {ordinal}.\n",
            encoding="utf-8",
        )

        first = build_managed_preflight(client, page_id, path)
        second = build_managed_preflight(client, page_id, path)

        assert first.proof_mode == "exact_remote_prefix_append"
        assert first.consent_required is False
        assert first.candidate_storage.startswith(client.storage)
        assert first.candidate_storage[: len(client.storage)] == client.storage
        assert first.append_sha256 == second.append_sha256
        assert first.migration_report_sha256 == second.migration_report_sha256
        assert first.ownership["unclassified"] == []
        assert client.puts == 0
