"""Small cross-platform primitives for fail-closed file publication."""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from pathlib import Path


def promote_file_no_replace(source: Path, destination: Path) -> None:
    """Move ``source`` into an expected-absent destination without clobbering it."""

    if os.name == "nt":
        # Windows rename fails when the destination already exists.
        os.rename(source, destination)
        return
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        at_fdcwd = -100
        rename_noreplace = 1
        result = renameat2(
            at_fdcwd,
            os.fsencode(source),
            at_fdcwd,
            os.fsencode(destination),
            rename_noreplace,
        )
        if result != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number), destination)
        return
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is None:
            raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        rename_excl = 0x00000004
        result = renamex_np(
            os.fsencode(source),
            os.fsencode(destination),
            rename_excl,
        )
        if result != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number), destination)
        return
    raise OSError(errno.ENOTSUP, "atomic no-replace rename is unsupported on this platform")


__all__ = ["promote_file_no_replace"]
