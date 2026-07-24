"""Descriptor-backed directory capabilities for race-resistant local publication."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import ntpath
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO

from atlassian_skills.core.errors import ConflictError, ValidationError

_REPARSE_POINT_FLAG = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))

_WIN_DELETE = 0x00010000
_WIN_SYNCHRONIZE = 0x00100000
_WIN_FILE_READ_DATA = 0x00000001
_WIN_FILE_WRITE_DATA = 0x00000002
_WIN_FILE_TRAVERSE = 0x00000020
_WIN_FILE_READ_ATTRIBUTES = 0x00000080
_WIN_FILE_WRITE_ATTRIBUTES = 0x00000100
_WIN_FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
_WIN_FILE_OPEN = 1
_WIN_FILE_CREATE = 2
_WIN_FILE_OPEN_IF = 3
_WIN_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_WIN_FILE_DIRECTORY_FILE = 0x00000001
_WIN_FILE_NON_DIRECTORY_FILE = 0x00000040
_WIN_FILE_OPEN_REPARSE_POINT = 0x00200000
_WIN_FILE_RENAME_INFORMATION = 10
_WIN_FILE_DISPOSITION_INFORMATION = 13

_NT_STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
_NT_STATUS_OBJECT_NAME_COLLISION = 0xC0000035
_NT_STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
_NT_STATUS_NOT_SAME_DEVICE = 0xC00000D4


@dataclass(frozen=True)
class DirectoryIdentity:
    platform: str
    primary: int
    secondary: int

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.primary:x}:{self.secondary:x}"


@dataclass(frozen=True)
class WindowsDirectoryHandle:
    handle: int
    volume_serial: int
    file_id: int
    reparse_point: bool


def _platform_name() -> str:
    return "windows" if os.name == "nt" else "posix"


def _directory_cache_key(path: Path) -> str:
    value = str(path)
    return ntpath.normcase(value) if _platform_name() == "windows" else value


def _directory_error(message: str, path: Path, *, reason: str, failure: str | None = None) -> ValidationError:
    context = {"reason": reason, "path": str(path)}
    if failure is not None:
        context["failure"] = failure
    return ValidationError(message, context=context)


def _is_reparse(info: os.stat_result) -> bool:
    return bool(int(getattr(info, "st_file_attributes", 0)) & _REPARSE_POINT_FLAG)


def _windows_handle_observation(handle: int) -> WindowsDirectoryHandle:
    if os.name != "nt":
        raise OSError("Windows directory handles are unavailable on this platform")

    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    win_dll: Any = ctypes.__dict__["WinDLL"]
    get_last_error: Any = ctypes.__dict__["get_last_error"]
    win_error: Any = ctypes.__dict__["WinError"]
    kernel32 = win_dll("kernel32", use_last_error=True)
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation)]
    get_information.restype = wintypes.BOOL
    information = ByHandleFileInformation()
    if not get_information(wintypes.HANDLE(handle), ctypes.byref(information)):
        raise win_error(get_last_error())
    file_id = (int(information.file_index_high) << 32) | int(information.file_index_low)
    return WindowsDirectoryHandle(
        handle=handle,
        volume_serial=int(information.volume_serial_number),
        file_id=file_id,
        reparse_point=bool(int(information.file_attributes) & _REPARSE_POINT_FLAG),
    )


def _open_windows_directory(path: Path) -> WindowsDirectoryHandle:
    if os.name != "nt":
        raise OSError("Windows directory handles are unavailable on this platform")

    from ctypes import wintypes

    win_dll: Any = ctypes.__dict__["WinDLL"]
    get_last_error: Any = ctypes.__dict__["get_last_error"]
    win_error: Any = ctypes.__dict__["WinError"]
    kernel32 = win_dll("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        _WIN_FILE_TRAVERSE | _WIN_FILE_READ_ATTRIBUTES | _WIN_SYNCHRONIZE,
        _WIN_FILE_SHARE_ALL,
        None,
        3,  # OPEN_EXISTING
        0x02000000 | _WIN_FILE_OPEN_REPARSE_POINT,  # FILE_FLAG_BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    assert invalid_handle is not None
    handle_value = int(handle) if handle is not None else invalid_handle
    if handle_value == invalid_handle:
        raise win_error(get_last_error())
    try:
        return _windows_handle_observation(handle_value)
    except BaseException:
        _close_windows_handle(handle_value)
        raise


def _close_windows_handle(handle: int) -> None:
    if os.name != "nt":
        return
    from ctypes import wintypes

    win_dll: Any = ctypes.__dict__["WinDLL"]
    kernel32 = win_dll("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    close_handle(wintypes.HANDLE(handle))


def _raise_windows_ntstatus(status: int, leaf: str) -> None:
    normalized = status & 0xFFFFFFFF
    if normalized in {_NT_STATUS_OBJECT_NAME_NOT_FOUND, _NT_STATUS_OBJECT_PATH_NOT_FOUND}:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), leaf)
    if normalized == _NT_STATUS_OBJECT_NAME_COLLISION:
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), leaf)
    if normalized == _NT_STATUS_NOT_SAME_DEVICE:
        raise OSError(errno.EXDEV, os.strerror(errno.EXDEV), leaf)
    if os.name != "nt":
        raise OSError(errno.EIO, f"Windows NT operation failed with status 0x{normalized:08x}", leaf)

    win_dll: Any = ctypes.__dict__["WinDLL"]
    win_error: Any = ctypes.__dict__["WinError"]
    ntdll = win_dll("ntdll", use_last_error=True)
    status_to_error = ntdll.RtlNtStatusToDosError
    status_to_error.argtypes = [ctypes.c_long]
    status_to_error.restype = ctypes.c_ulong
    raise win_error(int(status_to_error(ctypes.c_long(status))))


def _nt_create_file_relative(
    directory_handle: int,
    leaf: str,
    *,
    desired_access: int,
    create_disposition: int,
    create_options: int,
    file_attributes: int = 0x00000080,
) -> int:
    """Open one leaf through an NT ``RootDirectory`` capability."""

    if os.name != "nt":
        raise OSError("Windows relative file handles are unavailable on this platform")

    from ctypes import wintypes

    class UnicodeString(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.USHORT),
            ("maximum_length", wintypes.USHORT),
            ("buffer", wintypes.LPWSTR),
        ]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.ULONG),
            ("root_directory", wintypes.HANDLE),
            ("object_name", ctypes.POINTER(UnicodeString)),
            ("attributes", wintypes.ULONG),
            ("security_descriptor", wintypes.LPVOID),
            ("security_quality_of_service", wintypes.LPVOID),
        ]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("status", wintypes.LPVOID), ("information", ctypes.c_size_t)]

    name_buffer = ctypes.create_unicode_buffer(leaf)
    name_bytes = len(leaf.encode("utf-16-le"))
    name = UnicodeString(name_bytes, name_bytes + 2, ctypes.cast(name_buffer, wintypes.LPWSTR))
    attributes = ObjectAttributes(
        ctypes.sizeof(ObjectAttributes),
        wintypes.HANDLE(directory_handle),
        ctypes.pointer(name),
        0x00000040,  # OBJ_CASE_INSENSITIVE
        None,
        None,
    )
    io_status = IoStatusBlock()
    result_handle = wintypes.HANDLE()
    win_dll: Any = ctypes.__dict__["WinDLL"]
    ntdll = win_dll("ntdll", use_last_error=True)
    nt_create_file = ntdll.NtCreateFile
    nt_create_file.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.ULONG,
        ctypes.POINTER(ObjectAttributes),
        ctypes.POINTER(IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.LPVOID,
        wintypes.ULONG,
    ]
    nt_create_file.restype = ctypes.c_long
    status = int(
        nt_create_file(
            ctypes.byref(result_handle),
            desired_access,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            file_attributes,
            _WIN_FILE_SHARE_ALL,
            create_disposition,
            create_options,
            None,
            0,
        )
    )
    if status < 0:
        _raise_windows_ntstatus(status, leaf)
    handle_value = result_handle.value
    if handle_value is None:
        raise OSError(errno.EIO, "NtCreateFile returned no handle", leaf)
    return int(handle_value)


def _windows_open_leaf_handle(
    directory_handle: int,
    leaf: str,
    *,
    writable: bool,
    create_new: bool,
    delete_access: bool = False,
) -> int:
    leaf = _validate_leaf(leaf)
    desired_access = _WIN_FILE_READ_ATTRIBUTES | _WIN_SYNCHRONIZE
    if writable:
        desired_access |= _WIN_FILE_WRITE_DATA | _WIN_FILE_WRITE_ATTRIBUTES
    elif not delete_access:
        desired_access |= _WIN_FILE_READ_DATA
    if delete_access:
        desired_access |= _WIN_DELETE
    disposition = _WIN_FILE_CREATE if create_new else _WIN_FILE_OPEN
    options = _WIN_FILE_NON_DIRECTORY_FILE | _WIN_FILE_OPEN_REPARSE_POINT | _WIN_FILE_SYNCHRONOUS_IO_NONALERT
    return _nt_create_file_relative(
        directory_handle,
        leaf,
        desired_access=desired_access,
        create_disposition=disposition,
        create_options=options,
    )


def _windows_open_child_directory_handle(
    directory_handle: int,
    leaf: str,
    *,
    create_if_missing: bool = True,
) -> WindowsDirectoryHandle:
    handle = _nt_create_file_relative(
        directory_handle,
        leaf,
        desired_access=_WIN_FILE_TRAVERSE | _WIN_FILE_READ_ATTRIBUTES | _WIN_SYNCHRONIZE,
        create_disposition=_WIN_FILE_OPEN_IF if create_if_missing else _WIN_FILE_OPEN,
        create_options=_WIN_FILE_DIRECTORY_FILE | _WIN_FILE_OPEN_REPARSE_POINT | _WIN_FILE_SYNCHRONOUS_IO_NONALERT,
        file_attributes=0x00000010,  # FILE_ATTRIBUTE_DIRECTORY
    )
    try:
        observed = _windows_handle_observation(handle)
        if observed.reparse_point:
            raise ValidationError(
                "Child publication directory cannot be a Windows reparse point",
                context={"reason": "directory_reparse_point", "leaf": leaf},
            )
        return observed
    except BaseException:
        _close_windows_handle(handle)
        raise


def _windows_handle_to_fd(handle: int, flags: int) -> int:
    if os.name != "nt":
        raise OSError("Windows file descriptors are unavailable on this platform")
    import msvcrt

    open_osfhandle: Any = msvcrt.__dict__["open_osfhandle"]
    try:
        return int(open_osfhandle(handle, flags | getattr(os, "O_BINARY", 0)))
    except BaseException:
        _close_windows_handle(handle)
        raise


def _windows_open_leaf_descriptor(
    directory_handle: int,
    leaf: str,
    *,
    writable: bool = False,
    create_new: bool = False,
) -> int:
    handle = _windows_open_leaf_handle(
        directory_handle,
        leaf,
        writable=writable,
        create_new=create_new,
    )
    return _windows_handle_to_fd(handle, os.O_WRONLY if writable else os.O_RDONLY)


def _windows_set_rename(
    source_handle: int,
    destination_directory_handle: int,
    destination_leaf: str,
    *,
    replace: bool,
) -> None:
    if os.name != "nt":
        raise OSError("Windows relative rename is unavailable on this platform")

    from ctypes import wintypes

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("status", wintypes.LPVOID), ("information", ctypes.c_size_t)]

    class FileRenameInformation(ctypes.Structure):
        _fields_ = [
            ("replace_if_exists", wintypes.BOOLEAN),
            ("root_directory", wintypes.HANDLE),
            ("file_name_length", wintypes.ULONG),
            ("file_name", wintypes.WCHAR * 1),
        ]

    encoded_name = destination_leaf.encode("utf-16-le")
    header_size = FileRenameInformation.file_name.offset
    buffer = ctypes.create_string_buffer(ctypes.sizeof(FileRenameInformation) + len(encoded_name))
    information = ctypes.cast(buffer, ctypes.POINTER(FileRenameInformation)).contents
    information.replace_if_exists = bool(replace)
    information.root_directory = wintypes.HANDLE(destination_directory_handle)
    information.file_name_length = len(encoded_name)
    ctypes.memmove(ctypes.addressof(buffer) + header_size, encoded_name, len(encoded_name))

    io_status = IoStatusBlock()
    win_dll: Any = ctypes.__dict__["WinDLL"]
    ntdll = win_dll("ntdll", use_last_error=True)
    nt_set_information = ntdll.NtSetInformationFile
    nt_set_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    nt_set_information.restype = ctypes.c_long
    status = int(
        nt_set_information(
            wintypes.HANDLE(source_handle),
            ctypes.byref(io_status),
            buffer,
            len(buffer),
            _WIN_FILE_RENAME_INFORMATION,
        )
    )
    if status < 0:
        _raise_windows_ntstatus(status, destination_leaf)


def _windows_rename_relative(
    source_directory_handle: int,
    source_leaf: str,
    destination_directory_handle: int,
    destination_leaf: str,
    *,
    replace: bool,
) -> None:
    source_leaf = _validate_leaf(source_leaf)
    destination_leaf = _validate_leaf(destination_leaf)
    source_handle = _windows_open_leaf_handle(
        source_directory_handle,
        source_leaf,
        writable=False,
        create_new=False,
        delete_access=True,
    )
    try:
        observed = _windows_handle_observation(source_handle)
        if observed.reparse_point:
            raise ValidationError(
                "Directory capability leaf cannot be a symbolic link or reparse point",
                context={"reason": "unsafe_directory_capability_leaf", "leaf": source_leaf},
            )
        _windows_set_rename(
            source_handle,
            destination_directory_handle,
            destination_leaf,
            replace=replace,
        )
    finally:
        _close_windows_handle(source_handle)


def _windows_set_delete(handle: int, leaf: str) -> None:
    if os.name != "nt":
        raise OSError("Windows handle-relative delete is unavailable on this platform")

    from ctypes import wintypes

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("status", wintypes.LPVOID), ("information", ctypes.c_size_t)]

    class FileDispositionInformation(ctypes.Structure):
        _fields_ = [("delete_file", wintypes.BOOLEAN)]

    io_status = IoStatusBlock()
    information = FileDispositionInformation(True)
    win_dll: Any = ctypes.__dict__["WinDLL"]
    ntdll = win_dll("ntdll", use_last_error=True)
    nt_set_information = ntdll.NtSetInformationFile
    nt_set_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    nt_set_information.restype = ctypes.c_long
    status = int(
        nt_set_information(
            wintypes.HANDLE(handle),
            ctypes.byref(io_status),
            ctypes.byref(information),
            ctypes.sizeof(information),
            _WIN_FILE_DISPOSITION_INFORMATION,
        )
    )
    if status < 0:
        _raise_windows_ntstatus(status, leaf)


def _windows_unlink_relative(directory_handle: int, leaf: str) -> None:
    leaf = _validate_leaf(leaf)
    handle = _windows_open_leaf_handle(
        directory_handle,
        leaf,
        writable=False,
        create_new=False,
        delete_access=True,
    )
    try:
        observed = _windows_handle_observation(handle)
        if observed.reparse_point:
            raise ValidationError(
                "Directory capability leaf cannot be a symbolic link or reparse point",
                context={"reason": "unsafe_directory_capability_leaf", "leaf": leaf},
            )
        _windows_set_delete(handle, leaf)
    finally:
        _close_windows_handle(handle)


def _validate_leaf(leaf: str) -> str:
    if not leaf or leaf in {".", ".."} or "/" in leaf or "\\" in leaf or "\x00" in leaf:
        raise ValidationError(
            "Directory capability accepts one ordinary leaf name",
            context={"reason": "invalid_directory_capability_leaf", "leaf": leaf},
        )
    return leaf


def _renameat2_no_replace(source_fd: int, source_leaf: str, destination_fd: int, destination_leaf: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_fd,
        os.fsencode(source_leaf),
        destination_fd,
        os.fsencode(destination_leaf),
        1,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number), destination_leaf)
    raise OSError(error_number, os.strerror(error_number), destination_leaf)


class DirectoryCapability:
    """An opened directory plus the identity observed at acquisition."""

    def __init__(
        self,
        *,
        requested_directory: Path,
        directory: Path,
        identity: DirectoryIdentity,
        directory_fd: int | None,
        native_handle: int | None,
    ) -> None:
        self.requested_directory = requested_directory
        self.directory = directory
        self.identity = identity
        self.directory_fd = directory_fd
        self.native_handle = native_handle
        self._closed = False

    @classmethod
    def acquire(cls, directory: str | Path) -> DirectoryCapability:
        requested = Path(directory).expanduser().absolute()
        try:
            resolved = requested.resolve(strict=True)
            info = resolved.stat(follow_symlinks=False)
        except OSError as error:
            raise _directory_error(
                "Publication directory capability could not be acquired",
                requested,
                reason="directory_capability_unavailable",
                failure=type(error).__name__,
            ) from error
        if not stat.S_ISDIR(info.st_mode):
            raise _directory_error(
                "Publication capability target is not a directory",
                resolved,
                reason="directory_capability_not_directory",
            )
        if _is_reparse(info):
            raise _directory_error(
                "Publication directory cannot be a Windows reparse point",
                resolved,
                reason="directory_reparse_point",
            )

        if _platform_name() == "windows":
            try:
                observed = _open_windows_directory(resolved)
            except OSError as error:
                raise _directory_error(
                    "Windows directory capability could not open a stable handle",
                    resolved,
                    reason="directory_capability_unavailable",
                    failure=type(error).__name__,
                ) from error
            if observed.reparse_point:
                _close_windows_handle(observed.handle)
                raise _directory_error(
                    "Publication directory cannot be a Windows reparse point",
                    resolved,
                    reason="directory_reparse_point",
                )
            if not observed.volume_serial or not observed.file_id:
                _close_windows_handle(observed.handle)
                raise _directory_error(
                    "Windows directory did not provide a stable file identity",
                    resolved,
                    reason="directory_identity_unavailable",
                )
            return cls(
                requested_directory=requested,
                directory=resolved,
                identity=DirectoryIdentity("windows", observed.volume_serial, observed.file_id),
                directory_fd=None,
                native_handle=observed.handle,
            )

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(resolved, flags)
        except OSError as error:
            raise _directory_error(
                "POSIX directory capability could not open a stable descriptor",
                resolved,
                reason="directory_capability_unavailable",
                failure=type(error).__name__,
            ) from error
        try:
            observed_info = os.fstat(descriptor)
        except OSError as error:
            os.close(descriptor)
            raise _directory_error(
                "POSIX directory capability could not read its stable identity",
                resolved,
                reason="directory_capability_unavailable",
                failure=type(error).__name__,
            ) from error
        if not stat.S_ISDIR(observed_info.st_mode):
            os.close(descriptor)
            raise _directory_error(
                "Publication capability target is not a directory",
                resolved,
                reason="directory_capability_not_directory",
            )
        identity = DirectoryIdentity("posix", int(observed_info.st_dev), int(observed_info.st_ino))
        if (int(info.st_dev), int(info.st_ino)) != (identity.primary, identity.secondary):
            os.close(descriptor)
            raise _directory_error(
                "Publication directory changed while its capability was acquired",
                resolved,
                reason="directory_identity_changed",
            )
        return cls(
            requested_directory=requested,
            directory=resolved,
            identity=identity,
            directory_fd=descriptor,
            native_handle=None,
        )

    def __enter__(self) -> DirectoryCapability:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.directory_fd is not None:
            os.close(self.directory_fd)
            self.directory_fd = None
        if self.native_handle is not None:
            _close_windows_handle(self.native_handle)
            self.native_handle = None

    def revalidate(self) -> None:
        if self._closed:
            raise RuntimeError("directory capability is closed")
        try:
            path_info = self.directory.stat(follow_symlinks=False)
        except OSError as error:
            raise _directory_error(
                "Publication directory identity changed after acquisition",
                self.directory,
                reason="directory_identity_changed",
                failure=type(error).__name__,
            ) from error
        if not stat.S_ISDIR(path_info.st_mode) or _is_reparse(path_info):
            raise _directory_error(
                "Publication directory identity changed after acquisition",
                self.directory,
                reason="directory_identity_changed",
            )

        if self.identity.platform == "posix":
            descriptor = self.directory_fd
            if descriptor is None:
                raise RuntimeError("POSIX directory capability has no descriptor")
            handle_info = os.fstat(descriptor)
            handle_identity = (int(handle_info.st_dev), int(handle_info.st_ino))
            path_identity = (int(path_info.st_dev), int(path_info.st_ino))
        else:
            handle = self.native_handle
            if handle is None:
                raise RuntimeError("Windows directory capability has no handle")
            observed = _windows_handle_observation(handle)
            if observed.reparse_point:
                raise _directory_error(
                    "Publication directory became a Windows reparse point",
                    self.directory,
                    reason="directory_identity_changed",
                )
            handle_identity = (observed.volume_serial, observed.file_id)
            try:
                path_observed = _open_windows_directory(self.directory)
            except OSError as error:
                raise _directory_error(
                    "Publication directory identity changed after acquisition",
                    self.directory,
                    reason="directory_identity_changed",
                    failure=type(error).__name__,
                ) from error
            try:
                if path_observed.reparse_point:
                    raise _directory_error(
                        "Publication directory became a Windows reparse point",
                        self.directory,
                        reason="directory_identity_changed",
                    )
                path_identity = (path_observed.volume_serial, path_observed.file_id)
            finally:
                _close_windows_handle(path_observed.handle)
        expected = (self.identity.primary, self.identity.secondary)
        if handle_identity != expected or path_identity != expected:
            raise _directory_error(
                "Publication directory identity changed after acquisition",
                self.directory,
                reason="directory_identity_changed",
            )

    def path_for_leaf(self, leaf: str) -> Path:
        return self.directory / _validate_leaf(leaf)

    def acquire_child_directory(
        self,
        leaf: str,
        *,
        mode: int = 0o700,
        create: bool = True,
    ) -> DirectoryCapability:
        """Create/open one direct child without resolving the parent pathname again."""

        leaf = _validate_leaf(leaf)
        self.revalidate()
        requested = self.requested_directory / leaf
        directory = self.directory / leaf
        if self.identity.platform == "windows":
            parent_handle = self.native_handle
            if parent_handle is None:
                raise RuntimeError("Windows directory capability has no handle")
            observed = _windows_open_child_directory_handle(parent_handle, leaf, create_if_missing=create)
            try:
                self.revalidate()
            except BaseException:
                _close_windows_handle(observed.handle)
                raise
            return DirectoryCapability(
                requested_directory=requested,
                directory=directory,
                identity=DirectoryIdentity("windows", observed.volume_serial, observed.file_id),
                directory_fd=None,
                native_handle=observed.handle,
            )
        descriptor = self.directory_fd
        if descriptor is None:
            raise RuntimeError("POSIX directory capability has no descriptor")
        if create:
            with suppress(FileExistsError):
                os.mkdir(leaf, mode=mode, dir_fd=descriptor)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        child_descriptor = os.open(leaf, flags, dir_fd=descriptor)
        try:
            observed_info = os.fstat(child_descriptor)
            if not stat.S_ISDIR(observed_info.st_mode):
                raise ValidationError(
                    "Child publication capability target is not a directory",
                    context={"reason": "directory_capability_not_directory", "path": str(directory)},
                )
            self.revalidate()
        except BaseException:
            os.close(child_descriptor)
            raise
        return DirectoryCapability(
            requested_directory=requested,
            directory=directory,
            identity=DirectoryIdentity("posix", int(observed_info.st_dev), int(observed_info.st_ino)),
            directory_fd=child_descriptor,
            native_handle=None,
        )

    def _leaf_path(self, leaf: str) -> Path:
        self.revalidate()
        return self.path_for_leaf(leaf)

    def lstat_leaf(self, leaf: str) -> os.stat_result:
        leaf = _validate_leaf(leaf)
        self.revalidate()
        if self.directory_fd is not None:
            return os.stat(leaf, dir_fd=self.directory_fd, follow_symlinks=False)
        handle = self.native_handle
        if handle is None:
            raise RuntimeError("directory capability has neither a descriptor nor a Windows handle")
        descriptor = _windows_open_leaf_descriptor(handle, leaf)
        try:
            return os.fstat(descriptor)
        finally:
            os.close(descriptor)

    def leaf_exists(self, leaf: str) -> bool:
        try:
            self.lstat_leaf(leaf)
        except FileNotFoundError:
            return False
        return True

    def _open_leaf_readonly(self, leaf: str) -> int:
        leaf = _validate_leaf(leaf)
        self.revalidate()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            if self.directory_fd is not None:
                descriptor = os.open(leaf, flags, dir_fd=self.directory_fd)
            else:
                handle = self.native_handle
                if handle is None:
                    raise RuntimeError("Windows directory capability has no handle")
                descriptor = _windows_open_leaf_descriptor(handle, leaf)
        except OSError as error:
            try:
                info = self.lstat_leaf(leaf)
            except OSError:
                raise
            if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                raise ValidationError(
                    "Directory capability leaf cannot be a symbolic link or reparse point",
                    context={"reason": "unsafe_directory_capability_leaf", "path": str(self.path_for_leaf(leaf))},
                ) from error
            raise
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(descriptor)
            raise ValidationError(
                "Directory capability leaf must be one ordinary, singly-linked file",
                context={"reason": "unsafe_directory_capability_leaf", "path": str(self.path_for_leaf(leaf))},
            )
        return descriptor

    def file_identity(self, leaf: str) -> str:
        descriptor = self._open_leaf_readonly(leaf)
        try:
            info = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        platform = "windows" if self.identity.platform == "windows" else "posix"
        return f"{platform}:{int(info.st_dev):x}:{int(info.st_ino):x}"

    @contextmanager
    def open_readonly(
        self,
        leaf: str,
        *,
        expected_identity: str | None = None,
        expected_sha256: str | None = None,
    ) -> Iterator[BinaryIO]:
        """Open one proven leaf and retain its inode/handle for the whole consumer operation."""

        descriptor = self._open_leaf_readonly(leaf)
        info = os.fstat(descriptor)
        platform = "windows" if self.identity.platform == "windows" else "posix"
        observed_identity = f"{platform}:{int(info.st_dev):x}:{int(info.st_ino):x}"
        if expected_identity is not None and observed_identity != expected_identity:
            os.close(descriptor)
            raise ValidationError(
                "Directory capability leaf identity changed before it could be consumed",
                context={
                    "reason": "directory_capability_leaf_identity_changed",
                    "path": str(self.path_for_leaf(leaf)),
                    "expected_identity": expected_identity,
                    "observed_identity": observed_identity,
                },
            )
        try:
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                if expected_sha256 is not None:
                    digest = hashlib.sha256()
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                    observed_sha256 = digest.hexdigest()
                    stream.seek(0)
                    if observed_sha256 != expected_sha256:
                        raise ValidationError(
                            "Directory capability leaf content changed before it could be consumed",
                            context={
                                "reason": "directory_capability_leaf_content_changed",
                                "path": str(self.path_for_leaf(leaf)),
                                "expected_sha256": expected_sha256,
                                "observed_sha256": observed_sha256,
                            },
                        )
                yield stream
        except BaseException:
            with suppress(OSError):
                os.close(descriptor)
            raise
        self.revalidate()

    def sha256(self, leaf: str) -> str:
        descriptor = self._open_leaf_readonly(leaf)
        digest = hashlib.sha256()
        try:
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except BaseException:
            with suppress(OSError):
                os.close(descriptor)
            raise
        return digest.hexdigest()

    def write_bytes_exclusive(self, leaf: str, content: bytes, *, mode: int = 0o600) -> str:
        leaf = _validate_leaf(leaf)
        self.revalidate()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        if self.directory_fd is not None:
            descriptor = os.open(leaf, flags, mode, dir_fd=self.directory_fd)
        else:
            handle = self.native_handle
            if handle is None:
                raise RuntimeError("Windows directory capability has no handle")
            descriptor = _windows_open_leaf_descriptor(handle, leaf, writable=True, create_new=True)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            with suppress(OSError):
                os.close(descriptor)
            raise
        self.revalidate()
        return self.file_identity(leaf)

    def chmod_leaf(self, leaf: str, mode: int) -> None:
        descriptor = self._open_leaf_readonly(leaf)
        try:
            os.fchmod(descriptor, mode)
        finally:
            os.close(descriptor)

    def promote_no_replace(self, source_leaf: str, destination_leaf: str) -> None:
        source_leaf = _validate_leaf(source_leaf)
        destination_leaf = _validate_leaf(destination_leaf)
        self.revalidate()
        if self.directory_fd is not None:
            os.link(
                source_leaf,
                destination_leaf,
                src_dir_fd=self.directory_fd,
                dst_dir_fd=self.directory_fd,
                follow_symlinks=False,
            )
            os.unlink(source_leaf, dir_fd=self.directory_fd)
        else:
            handle = self.native_handle
            if handle is None:
                raise RuntimeError("Windows directory capability has no handle")
            _windows_rename_relative(handle, source_leaf, handle, destination_leaf, replace=False)
        self.revalidate()

    def rename_to_no_replace(
        self,
        source_leaf: str,
        destination: DirectoryCapability,
        destination_leaf: str,
    ) -> None:
        """Atomically rename one anchored leaf across same-volume directory capabilities."""

        source_leaf = _validate_leaf(source_leaf)
        destination_leaf = _validate_leaf(destination_leaf)
        self.revalidate()
        destination.revalidate()
        if (
            self.identity.platform != destination.identity.platform
            or self.identity.primary != destination.identity.primary
        ):
            raise ConflictError(
                "Relocation requires source and destination on the same volume",
                context={
                    "reason": "relocate_cross_volume",
                    "source_directory": str(self.directory),
                    "destination_directory": str(destination.directory),
                },
            )
        if self.identity.platform == "windows":
            source_handle = self.native_handle
            destination_handle = destination.native_handle
            if source_handle is None or destination_handle is None:
                raise RuntimeError("Windows directory capability has no handle")
            source_identity = self.file_identity(source_leaf)
            try:
                _windows_rename_relative(
                    source_handle,
                    source_leaf,
                    destination_handle,
                    destination_leaf,
                    replace=False,
                )
            except OSError as error:
                if error.errno == errno.EXDEV:
                    raise ConflictError(
                        "Relocation requires source and destination on the same volume",
                        context={"reason": "relocate_cross_volume"},
                    ) from error
                raise
            self.revalidate()
            destination.revalidate()
            if self.leaf_exists(source_leaf) or destination.file_identity(destination_leaf) != source_identity:
                raise ValidationError(
                    "Atomic relocation read-back did not preserve file identity",
                    context={"reason": "relocate_identity_mismatch"},
                )
            self.fsync()
            if destination is not self:
                destination.fsync()
            return
        if self.identity.platform != "posix":
            raise RuntimeError(f"unsupported directory capability platform: {self.identity.platform}")
        source_fd = self.directory_fd
        destination_fd = destination.directory_fd
        if source_fd is None or destination_fd is None:
            raise RuntimeError("POSIX directory capability has no descriptor")
        source_info = self.lstat_leaf(source_leaf)
        if source_info is None or not stat.S_ISREG(source_info.st_mode):
            raise ValidationError(
                "Relocation source is missing or is not a regular file",
                context={"reason": "relocate_source_invalid", "leaf": source_leaf},
            )
        if destination.leaf_exists(destination_leaf):
            raise FileExistsError(destination.path_for_leaf(destination_leaf))
        source_identity = self.file_identity(source_leaf)
        try:
            _renameat2_no_replace(source_fd, source_leaf, destination_fd, destination_leaf)
        except OSError as error:
            if error.errno == errno.EXDEV:
                raise ConflictError(
                    "Relocation requires source and destination on the same volume",
                    context={"reason": "relocate_cross_volume"},
                ) from error
            if error.errno in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
                raise ValidationError(
                    "The filesystem does not provide anchored atomic no-replace rename",
                    context={"reason": "directory_atomic_relocate_unavailable"},
                ) from error
            raise
        self.revalidate()
        destination.revalidate()
        if self.leaf_exists(source_leaf) or destination.file_identity(destination_leaf) != source_identity:
            raise ValidationError(
                "Atomic relocation read-back did not preserve file identity",
                context={"reason": "relocate_identity_mismatch"},
            )
        self.fsync()
        if destination is not self:
            destination.fsync()

    def replace(self, source_leaf: str, destination_leaf: str) -> None:
        source_leaf = _validate_leaf(source_leaf)
        destination_leaf = _validate_leaf(destination_leaf)
        self.revalidate()
        if self.directory_fd is not None:
            os.replace(
                source_leaf,
                destination_leaf,
                src_dir_fd=self.directory_fd,
                dst_dir_fd=self.directory_fd,
            )
        else:
            handle = self.native_handle
            if handle is None:
                raise RuntimeError("Windows directory capability has no handle")
            _windows_rename_relative(handle, source_leaf, handle, destination_leaf, replace=True)
        self.revalidate()
        self.fsync()

    def unlink(self, leaf: str, *, missing_ok: bool = False) -> None:
        leaf = _validate_leaf(leaf)
        self.revalidate()
        try:
            if self.directory_fd is not None:
                os.unlink(leaf, dir_fd=self.directory_fd)
            else:
                handle = self.native_handle
                if handle is None:
                    raise RuntimeError("Windows directory capability has no handle")
                _windows_unlink_relative(handle, leaf)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def fsync(self) -> None:
        self.revalidate()
        if self.directory_fd is not None:
            os.fsync(self.directory_fd)


class DirectoryCapabilityPool:
    """Own and close one capability for each requested directory."""

    def __init__(self) -> None:
        self._capabilities: dict[str, DirectoryCapability] = {}

    def __enter__(self) -> DirectoryCapabilityPool:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def acquire(self, directory: str | Path) -> DirectoryCapability:
        requested = Path(directory).expanduser().absolute()
        requested_key = _directory_cache_key(requested)
        capability = self._capabilities.get(requested_key)
        if capability is None:
            capability = DirectoryCapability.acquire(requested)
            resolved_key = _directory_cache_key(capability.directory)
            existing = self._capabilities.get(resolved_key)
            if existing is not None:
                capability.close()
                capability = existing
            self._capabilities[requested_key] = capability
            self._capabilities[resolved_key] = capability
        return capability

    def acquire_child(
        self,
        parent: DirectoryCapability,
        leaf: str,
        *,
        mode: int = 0o700,
    ) -> DirectoryCapability:
        leaf = _validate_leaf(leaf)
        requested_key = _directory_cache_key(parent.requested_directory / leaf)
        capability = self._capabilities.get(requested_key)
        if capability is not None:
            capability.revalidate()
            return capability
        capability = parent.acquire_child_directory(leaf, mode=mode)
        resolved_key = _directory_cache_key(capability.directory)
        existing = self._capabilities.get(resolved_key)
        if existing is not None:
            capability.close()
            capability = existing
        self._capabilities[requested_key] = capability
        self._capabilities[resolved_key] = capability
        return capability

    def close(self) -> None:
        capabilities = tuple(dict.fromkeys(self._capabilities.values()))
        for capability in reversed(capabilities):
            capability.close()
        self._capabilities.clear()


__all__ = [
    "DirectoryCapability",
    "DirectoryCapabilityPool",
    "DirectoryIdentity",
    "WindowsDirectoryHandle",
]
