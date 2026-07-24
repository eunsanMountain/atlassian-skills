from __future__ import annotations

import os
from types import SimpleNamespace
from typing import cast

from atlassian_skills.core.file_identity import _has_windows_reparse_attribute


def test_windows_reparse_attribute_is_detected_without_native_path_access() -> None:
    ordinary = cast(os.stat_result, SimpleNamespace(st_file_attributes=0))
    reparse = cast(os.stat_result, SimpleNamespace(st_file_attributes=0x0400))

    assert _has_windows_reparse_attribute(ordinary) is False
    assert _has_windows_reparse_attribute(reparse) is True
