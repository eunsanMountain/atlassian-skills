from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import atlassian_skills.core.directory_capability as directory_capability
from atlassian_skills.confluence.client import ConfluenceClient
from atlassian_skills.confluence.pull_md import pull_md, pull_pages_batch
from atlassian_skills.core.attachment_io import AttachmentWriteBatch, resolve_attachment_writer
from atlassian_skills.core.directory_capability import (
    DirectoryCapability,
    DirectoryCapabilityPool,
    DirectoryIdentity,
    WindowsDirectoryHandle,
)
from atlassian_skills.core.errors import ValidationError
from atlassian_skills.jira.client import JiraClient


def test_open_readonly_rejects_leaf_replacement_after_staging(tmp_path: Path) -> None:
    root = tmp_path / "stage"
    root.mkdir()
    with DirectoryCapability.acquire(root) as capability:
        expected_identity = capability.write_bytes_exclusive("asset.bin", b"trusted")
        staged = root / "asset.bin"
        staged.unlink()
        staged.write_bytes(b"replacement")

        with (
            pytest.raises(ValidationError) as exc_info,
            capability.open_readonly(
                "asset.bin",
                expected_identity=expected_identity,
                expected_sha256="a9a089195c68d2adeee23beaa2c3a93b1d4cdf09046e7a9e520b3b166dff3e6a",
            ),
        ):
            pass

    assert exc_info.value.context["reason"] in {
        "directory_capability_leaf_identity_changed",
        "directory_capability_leaf_content_changed",
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd contract")
def test_cross_directory_atomic_rename_is_anchored_and_no_replace(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    destination_dir = tmp_path / "destination"
    source_dir.mkdir()
    destination_dir.mkdir()
    (source_dir / "page.md").write_text("managed", encoding="utf-8")

    with DirectoryCapability.acquire(source_dir) as source, DirectoryCapability.acquire(destination_dir) as destination:
        identity = source.file_identity("page.md")
        source.rename_to_no_replace("page.md", destination, "page.md")
        assert destination.file_identity("page.md") == identity
        assert not (source_dir / "page.md").exists()
        (source_dir / "other.md").write_text("other", encoding="utf-8")
        with pytest.raises(FileExistsError):
            source.rename_to_no_replace("other.md", destination, "page.md")


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd contract")
def test_cross_directory_atomic_rename_rejects_parent_swap(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    destination_dir = tmp_path / "destination"
    displaced = tmp_path / "displaced"
    source_dir.mkdir()
    destination_dir.mkdir()
    (source_dir / "page.md").write_text("managed", encoding="utf-8")

    with DirectoryCapability.acquire(source_dir) as source, DirectoryCapability.acquire(destination_dir) as destination:
        destination_dir.rename(displaced)
        destination_dir.mkdir()
        with pytest.raises(ValidationError) as exc_info:
            source.rename_to_no_replace("page.md", destination, "page.md")

    assert exc_info.value.context["reason"] == "directory_identity_changed"
    assert not (destination_dir / "page.md").exists()
    assert not (displaced / "page.md").exists()
    assert (source_dir / "page.md").read_text(encoding="utf-8") == "managed"


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd contract")
def test_attachment_batch_parent_symlink_swap_cannot_redirect_write(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    outside = tmp_path / "outside"
    managed.mkdir()
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(managed, target_is_directory=True)
    batch = AttachmentWriteBatch(resolve_attachment_writer(linked))

    linked.unlink()
    linked.symlink_to(outside, target_is_directory=True)
    batch.add(linked / "asset.bin", b"managed")
    batch.commit()

    assert (managed / "asset.bin").read_bytes() == b"managed"
    assert not (outside / "asset.bin").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd contract")
def test_attachment_download_binds_parent_before_network_fetch(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    outside = tmp_path / "outside"
    managed.mkdir()
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(managed, target_is_directory=True)

    class SwapClient:
        def fetch_attachment_bytes(self, _attachment_id: str, _download_link: str | None) -> bytes:
            linked.unlink()
            linked.symlink_to(outside, target_is_directory=True)
            return b"managed"

    result = ConfluenceClient.download_attachment(SwapClient(), "attachment-1", linked / "asset.bin")  # type: ignore[arg-type]

    assert result == linked / "asset.bin"
    assert (managed / "asset.bin").read_bytes() == b"managed"
    assert not (outside / "asset.bin").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd contract")
@pytest.mark.parametrize("product", ["confluence", "jira"])
def test_bulk_attachment_download_binds_parent_before_remote_listing(tmp_path: Path, product: str) -> None:
    managed = tmp_path / "managed"
    outside = tmp_path / "outside"
    managed.mkdir()
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(managed, target_is_directory=True)

    class SwapClient:
        def _swap(self) -> None:
            linked.unlink()
            linked.symlink_to(outside, target_is_directory=True)

        def list_attachments(self, _page_id: str) -> list[SimpleNamespace]:
            self._swap()
            return [SimpleNamespace(id="attachment-1", title="asset.bin", links=None)]

        def fetch_attachment_bytes(self, _attachment_id: str, _download_link: str | None) -> bytes:
            return b"managed"

        def get_attachment_content(self, _issue_key: str) -> list[SimpleNamespace]:
            self._swap()
            return [SimpleNamespace(id="attachment-1", filename="asset.bin", content="/attachment-1")]

        def get(self, _url: str, *, max_response_bytes: int) -> SimpleNamespace:
            assert max_response_bytes > 0
            return SimpleNamespace(content=b"managed")

    if product == "confluence":
        ConfluenceClient.download_all_attachments(SwapClient(), "123456", linked)  # type: ignore[arg-type]
        assert (managed / "asset.bin").read_bytes() == b"managed"
    else:
        with pytest.raises(ValidationError):
            JiraClient.download_attachments(SwapClient(), "TEST-1", linked)  # type: ignore[arg-type]
        assert not (managed / "asset.bin").exists()

    assert not (outside / "asset.bin").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd contract")
def test_pull_binds_output_parent_before_remote_page_fetch(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    outside = tmp_path / "outside"
    managed.mkdir()
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(managed, target_is_directory=True)

    class SwapClient:
        def get_page(self, _page_id: str) -> SimpleNamespace:
            linked.unlink()
            linked.symlink_to(outside, target_is_directory=True)
            return SimpleNamespace(body_storage="<p>managed</p>", title="Managed", version=1)

    pull_md(SwapClient(), "page-1", output_path=linked / "page.md")

    assert (managed / "page.md").read_text(encoding="utf-8") == "managed\n"
    assert not (outside / "page.md").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd contract")
def test_batch_pull_binds_output_root_before_first_remote_page_fetch(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    displaced = tmp_path / "displaced"
    outside = tmp_path / "outside"
    output_root.mkdir()
    outside.mkdir()

    class SwapClient:
        def get_page(self, page_id: str) -> SimpleNamespace:
            output_root.rename(displaced)
            output_root.symlink_to(outside, target_is_directory=True)
            return SimpleNamespace(id=page_id, body_storage="<p>managed</p>", title="Managed", version=1)

    with pytest.raises(ValidationError) as exc_info:
        pull_pages_batch(SwapClient(), ["123456"], output_root)

    assert exc_info.value.context["reason"] == "directory_identity_changed"
    assert not any(displaced.iterdir())
    assert not any(outside.iterdir())


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd contract")
def test_windows_directory_capability_records_handle_and_file_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(directory_capability, "_platform_name", lambda: "windows")
    monkeypatch.setattr(
        directory_capability,
        "_open_windows_directory",
        lambda _path: WindowsDirectoryHandle(handle=123, volume_serial=45, file_id=67, reparse_point=False),
    )
    closed: list[int] = []
    monkeypatch.setattr(directory_capability, "_close_windows_handle", closed.append)

    capability = DirectoryCapability.acquire(tmp_path)

    assert capability.identity == DirectoryIdentity("windows", 45, 67)
    assert capability.native_handle == 123
    capability.close()
    assert closed == [123]


def test_windows_directory_capability_rejects_reparse_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(directory_capability, "_platform_name", lambda: "windows")
    monkeypatch.setattr(
        directory_capability,
        "_open_windows_directory",
        lambda _path: WindowsDirectoryHandle(handle=123, volume_serial=45, file_id=67, reparse_point=True),
    )
    closed: list[int] = []
    monkeypatch.setattr(directory_capability, "_close_windows_handle", closed.append)

    with pytest.raises(ValidationError) as exc_info:
        DirectoryCapability.acquire(tmp_path)

    assert exc_info.value.context["reason"] == "directory_reparse_point"
    assert closed == [123]


def test_windows_revalidate_compares_native_identity_for_handle_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = DirectoryCapability(
        requested_directory=tmp_path,
        directory=tmp_path,
        identity=DirectoryIdentity("windows", 45, 67),
        directory_fd=None,
        native_handle=123,
    )
    monkeypatch.setattr(
        directory_capability,
        "_windows_handle_observation",
        lambda handle: WindowsDirectoryHandle(handle=handle, volume_serial=45, file_id=67, reparse_point=False),
    )
    monkeypatch.setattr(
        directory_capability,
        "_open_windows_directory",
        lambda _path: WindowsDirectoryHandle(handle=456, volume_serial=45, file_id=67, reparse_point=False),
    )
    closed: list[int] = []
    monkeypatch.setattr(directory_capability, "_close_windows_handle", closed.append)

    capability.revalidate()

    assert closed == [456]


def test_windows_relative_open_contract_uses_root_handle_and_safe_nt_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, str, int, int, int]] = []

    def observe_open(
        directory_handle: int,
        leaf: str,
        *,
        desired_access: int,
        create_disposition: int,
        create_options: int,
    ) -> int:
        calls.append((directory_handle, leaf, desired_access, create_disposition, create_options))
        return 701 + len(calls)

    monkeypatch.setattr(directory_capability, "_nt_create_file_relative", observe_open)

    read_handle = directory_capability._windows_open_leaf_handle(
        55,
        "read.bin",
        writable=False,
        create_new=False,
    )
    create_handle = directory_capability._windows_open_leaf_handle(
        55,
        "create.bin",
        writable=True,
        create_new=True,
    )
    delete_handle = directory_capability._windows_open_leaf_handle(
        55,
        "delete.bin",
        writable=False,
        create_new=False,
        delete_access=True,
    )

    assert (read_handle, create_handle, delete_handle) == (702, 703, 704)
    read_call, create_call, delete_call = calls
    assert read_call[:2] == (55, "read.bin")
    assert read_call[2] & directory_capability._WIN_FILE_READ_DATA
    assert not read_call[2] & directory_capability._WIN_FILE_WRITE_DATA
    assert read_call[3] == directory_capability._WIN_FILE_OPEN
    assert create_call[2] & directory_capability._WIN_FILE_WRITE_DATA
    assert create_call[3] == directory_capability._WIN_FILE_CREATE
    assert delete_call[2] & directory_capability._WIN_DELETE
    assert not delete_call[2] & directory_capability._WIN_FILE_READ_DATA
    for call in calls:
        assert call[2] & directory_capability._WIN_FILE_READ_ATTRIBUTES
        assert call[2] & directory_capability._WIN_SYNCHRONIZE
        assert call[4] & directory_capability._WIN_FILE_NON_DIRECTORY_FILE
        assert call[4] & directory_capability._WIN_FILE_OPEN_REPARSE_POINT
        assert call[4] & directory_capability._WIN_FILE_SYNCHRONOUS_IO_NONALERT


def _fake_windows_capability(tmp_path: Path, *, handle: int, volume: int = 9) -> DirectoryCapability:
    return DirectoryCapability(
        requested_directory=tmp_path,
        directory=tmp_path,
        identity=DirectoryIdentity("windows", volume, handle),
        directory_fd=None,
        native_handle=handle,
    )


def test_windows_leaf_read_and_create_dispatch_through_directory_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = _fake_windows_capability(tmp_path, handle=88)
    monkeypatch.setattr(capability, "revalidate", lambda: None)
    source = tmp_path / "source.bin"
    source.write_bytes(b"read")
    read_fd = os.open(source, os.O_RDONLY)
    created = tmp_path / "created.bin"
    write_fd = os.open(created, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    calls: list[tuple[int, str, bool, bool]] = []

    def open_leaf(
        directory_handle: int,
        leaf: str,
        *,
        writable: bool = False,
        create_new: bool = False,
    ) -> int:
        calls.append((directory_handle, leaf, writable, create_new))
        return os.dup(write_fd if writable else read_fd)

    monkeypatch.setattr(directory_capability, "_windows_open_leaf_descriptor", open_leaf)
    monkeypatch.setattr(capability, "file_identity", lambda _leaf: "windows:9:1")

    assert capability.lstat_leaf("source.bin").st_size == 4
    assert capability.write_bytes_exclusive("new.bin", b"created") == "windows:9:1"
    os.close(read_fd)
    os.close(write_fd)

    assert created.read_bytes() == b"created"
    assert calls == [
        (88, "source.bin", False, False),
        (88, "new.bin", True, True),
    ]


def test_windows_replace_promote_and_unlink_are_handle_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = _fake_windows_capability(tmp_path, handle=91)
    monkeypatch.setattr(capability, "revalidate", lambda: None)
    renames: list[tuple[int, str, int, str, bool]] = []
    unlinks: list[tuple[int, str]] = []
    monkeypatch.setattr(
        directory_capability,
        "_windows_rename_relative",
        lambda source_handle, source_leaf, destination_handle, destination_leaf, *, replace: renames.append(
            (source_handle, source_leaf, destination_handle, destination_leaf, replace)
        ),
    )
    monkeypatch.setattr(
        directory_capability,
        "_windows_unlink_relative",
        lambda directory_handle, leaf: unlinks.append((directory_handle, leaf)),
    )

    capability.promote_no_replace("stage.bin", "published.bin")
    capability.replace("replacement.bin", "published.bin")
    capability.unlink("published.bin")

    assert renames == [
        (91, "stage.bin", 91, "published.bin", False),
        (91, "replacement.bin", 91, "published.bin", True),
    ]
    assert unlinks == [(91, "published.bin")]


def test_windows_native_rename_and_unlink_open_source_by_root_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opens: list[tuple[int, str, bool, bool, bool]] = []
    renames: list[tuple[int, int, str, bool]] = []
    deletes: list[tuple[int, str]] = []
    closed: list[int] = []

    def open_leaf(
        directory_handle: int,
        leaf: str,
        *,
        writable: bool,
        create_new: bool,
        delete_access: bool = False,
    ) -> int:
        opens.append((directory_handle, leaf, writable, create_new, delete_access))
        return 300 + len(opens)

    monkeypatch.setattr(directory_capability, "_windows_open_leaf_handle", open_leaf)
    monkeypatch.setattr(
        directory_capability,
        "_windows_handle_observation",
        lambda handle: WindowsDirectoryHandle(handle, 7, handle, False),
    )
    monkeypatch.setattr(
        directory_capability,
        "_windows_set_rename",
        lambda source_handle, destination_handle, destination_leaf, *, replace: renames.append(
            (source_handle, destination_handle, destination_leaf, replace)
        ),
    )
    monkeypatch.setattr(
        directory_capability,
        "_windows_set_delete",
        lambda handle, leaf: deletes.append((handle, leaf)),
    )
    monkeypatch.setattr(directory_capability, "_close_windows_handle", closed.append)

    directory_capability._windows_rename_relative(10, "source.bin", 20, "destination.bin", replace=False)
    directory_capability._windows_unlink_relative(10, "obsolete.bin")

    assert opens == [
        (10, "source.bin", False, False, True),
        (10, "obsolete.bin", False, False, True),
    ]
    assert renames == [(301, 20, "destination.bin", False)]
    assert deletes == [(302, "obsolete.bin")]
    assert closed == [301, 302]


def test_windows_cross_directory_relocate_is_atomic_no_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _fake_windows_capability(tmp_path / "source", handle=101, volume=7)
    destination = _fake_windows_capability(tmp_path / "destination", handle=202, volume=7)
    monkeypatch.setattr(source, "revalidate", lambda: None)
    monkeypatch.setattr(destination, "revalidate", lambda: None)
    monkeypatch.setattr(source, "file_identity", lambda _leaf: "windows:7:abc")
    monkeypatch.setattr(source, "leaf_exists", lambda _leaf: False)
    monkeypatch.setattr(destination, "file_identity", lambda _leaf: "windows:7:abc")
    monkeypatch.setattr(source, "fsync", lambda: None)
    monkeypatch.setattr(destination, "fsync", lambda: None)
    calls: list[tuple[int, str, int, str, bool]] = []
    monkeypatch.setattr(
        directory_capability,
        "_windows_rename_relative",
        lambda source_handle, source_leaf, destination_handle, destination_leaf, *, replace: calls.append(
            (source_handle, source_leaf, destination_handle, destination_leaf, replace)
        ),
    )

    source.rename_to_no_replace("page.md", destination, "moved.md")

    assert calls == [(101, "page.md", 202, "moved.md", False)]


def test_directory_capability_pool_reuses_resolved_alias(tmp_path: Path) -> None:
    real = tmp_path / "real"
    alias = tmp_path / "alias"
    real.mkdir()
    alias.symlink_to(real, target_is_directory=True)

    with DirectoryCapabilityPool() as pool:
        through_alias = pool.acquire(alias)
        through_real = pool.acquire(real)

    assert through_alias is through_real


def test_windows_directory_capability_pool_keys_are_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(directory_capability, "_platform_name", lambda: "windows")

    assert directory_capability._directory_cache_key(
        Path("C:/Managed/Page")
    ) == directory_capability._directory_cache_key(Path("c:/managed/page"))
