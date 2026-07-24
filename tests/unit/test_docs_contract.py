from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
SKILL = ROOT / "src" / "atlassian_skills" / "_assets" / "skills" / "atls" / "SKILL.md"


def test_bundled_skill_has_only_state_free_markdown_workflow() -> None:
    text = SKILL.read_text(encoding="utf-8")
    stale = (
        "SQLite",
        "protected_region",
        "protected-region",
        "needs_migration",
        "atls state",
        "table-style",
        "allow-stale-managed",
        "migration accept",
        "migration revoke",
        "binding marker",
    )

    assert not {token for token in stale if token.casefold() in text.casefold()}

    # Inversions of the managed/stateless and diff-local safety boundaries must
    # never appear (a doc edit that equates the two transports, or promotes
    # diff-local to a storage proof, is exactly the regression these guard).
    forbidden = (
        "same raw JSON",
        "identical raw JSON",
        "diff-local is a storage",
        "diff-local` is a storage",
    )
    assert not {token for token in forbidden if token.casefold() in text.casefold()}
    for token in (
        "page inspect",
        "pulled_with_migrations",
        "exact_remote_prefix_append",
        "migration_consent_required",
        "next_actions[].argv",
        "--accept-migration",
        "--accept-conversion",
        "upload_unknown",
        "readback_pending",
        "requires_user_approval=true",
        "--intent=structure-edit",
        "without an unnecessary get or repull",
        "Automatic asset synchronization exists only in `push-md`",
        "page update --body-format storage",
        "durable journal applies only to managed `push-md`",
        "exact cfxmark version is fingerprint input",
        # managed vs stateless transports differ; branch on outcome, not fields.
        "different raw JSON/`status`",
        "branch on outcome, not field equality",
        # diff-local is a local review aid, never a storage-candidate proof.
        "`diff-local` is a local Markdown diff, not a storage-candidate proof",
    ):
        assert token in text


def test_readme_and_migration_guide_match_public_cli_surface() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs" / "confluence-markdown-0.3-migration.md").read_text(encoding="utf-8")
    current = readme + guide

    for removed in (
        "atls confluence page migration list",
        "atls confluence page migration accept",
        "atls confluence page migration revoke",
        "atls state recover",
        "atls state relocate",
        "--allow-stale-managed",
    ):
        assert removed not in current
    for required in (
        "page get PAGE_ID --body-repr=view --format=raw",
        "page inspect PAGE_ID --format=json",
        "page validate-local page.md --format=json",
        "page push-md PAGE_ID",
        "--accept-migration",
        "--accept-conversion",
        "cfxmark>=0.5.0,<0.6",
        "outside Markdown-conversion",
    ):
        assert required in current


def test_design_records_portable_authority_and_recovery_boundaries() -> None:
    design = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")

    for required in (
        "S0/M0/C0/M1/C1",
        "exact_remote_prefix_append",
        "requires_user_approval=true",
        "remote_freshness=not_checked",
        "upload_unknown",
        "body_put_failed",
        "readback_pending",
        "There is no checkout registry",
        "no runtime SQLite state",
        "Live Atlassian writes",
        "journal is scoped to managed `push-md`",
        "page update --body-format=storage",
    ):
        assert required in design
