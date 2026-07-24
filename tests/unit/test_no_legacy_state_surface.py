from __future__ import annotations

import ast
import builtins
import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import atlassian_skills.cli.setup as setup_mod
from atlassian_skills.cli.main import app
from atlassian_skills.confluence.validate_local import validate_local
from atlassian_skills.core.managed_manifest import (
    ManagedManifest,
    canonical_asset_set_sha256,
    canonical_content_sha256,
    serialize_managed_manifest,
)

_DELETED_MODULES = {
    "atlassian_skills.cli.state",
    "atlassian_skills.core.state_store",
    "atlassian_skills.core.state_lifecycle",
    "atlassian_skills.core.operation_journal",
    "atlassian_skills.confluence.managed_state",
    "atlassian_skills.confluence.migration",
    "atlassian_skills.confluence.protected_regions",
    "atlassian_skills.confluence.presentation",
    "atlassian_skills.confluence.semantic",
    "atlassian_skills.confluence.table_style",
}


def _sqlite_header() -> bytes:
    header = bytearray(72)
    header[:16] = b"SQLite format 3\x00"
    header[68:72] = (0x41544C53).to_bytes(4, "big")
    return bytes(header) + b"not a real database"


def test_production_has_no_legacy_state_import_or_module() -> None:
    source_root = Path(__file__).parents[2] / "src" / "atlassian_skills"
    for module in _DELETED_MODULES:
        relative = Path(*module.split(".")[1:]).with_suffix(".py")
        assert not (source_root / relative).exists(), module
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names} | {
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert imports.isdisjoint(_DELETED_MODULES), path
        assert "sqlite3" not in imports, path


def test_cli_help_has_no_legacy_state_or_protected_lifecycle() -> None:
    root = CliRunner().invoke(app, ["--help"])
    page = CliRunner().invoke(app, ["confluence", "page", "--help"])

    assert root.exit_code == 0, root.output
    assert page.exit_code == 0, page.output
    assert "state" not in {line.strip().split()[0] for line in root.output.splitlines() if line.strip()}
    assert "migration" not in page.output
    assert "table-style" not in page.output
    assert "allow-stale-managed" not in page.output


def test_legacy_cleanup_dry_run_reads_only_header_and_does_not_import_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.sqlite3"
    database.write_bytes(_sqlite_header())
    before = database.stat()
    monkeypatch.setattr(setup_mod, "_legacy_state_path", lambda: database)
    original_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "sqlite3":
            raise AssertionError("legacy cleanup must not import the SQLite runtime")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    result = setup_mod._cleanup_legacy_state(dry_run=True)

    after = database.stat()
    assert result["status"] == "would_remove"
    assert database.read_bytes() == _sqlite_header()
    assert (after.st_ino, after.st_mtime_ns, after.st_size) == (before.st_ino, before.st_mtime_ns, before.st_size)


def test_legacy_cleanup_removes_only_exact_regular_database_and_companions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.sqlite3"
    database.write_bytes(_sqlite_header())
    companions = [Path(f"{database}-wal"), Path(f"{database}-shm")]
    for companion in companions:
        companion.write_bytes(b"legacy companion")
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(setup_mod, "_legacy_state_path", lambda: database)

    result = setup_mod._cleanup_legacy_state(dry_run=False)

    assert result["status"] == "removed"
    assert not database.exists()
    assert all(not companion.exists() for companion in companions)
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_legacy_cleanup_rejects_symlink_without_touching_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target.sqlite3"
    target.write_bytes(_sqlite_header())
    link = tmp_path / "state.sqlite3"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    monkeypatch.setattr(setup_mod, "_legacy_state_path", lambda: link)

    with pytest.raises(typer.BadParameter):
        setup_mod._cleanup_legacy_state(dry_run=False)

    assert target.read_bytes() == _sqlite_header()
    assert link.is_symlink()


def test_validate_local_uses_only_manifest_and_local_bytes(tmp_path: Path) -> None:
    body = "# Portable\n"
    manifest = ManagedManifest(
        v=2,
        page="123",
        site="sha256:" + "a" * 64,
        remote_version=7,
        remote_storage="sha256:" + "b" * 64,
        base_md=canonical_content_sha256(body),
        assets=canonical_asset_set_sha256(()),
        converter="cfxmark/0.5.0",
        profile="markdown-first",
    )
    path = tmp_path / "page.md"
    path.write_text(serialize_managed_manifest(manifest) + "\n" + body, encoding="utf-8")

    clean = validate_local(path)
    path.write_text(path.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
    dirty = validate_local(path)

    assert clean["body"]["dirty"] is False
    assert dirty["body"]["dirty"] is True
    assert clean["remote_freshness"] == "not_checked"
    assert clean["state_authority"] is False
    assert json.dumps(clean, sort_keys=True)
