"""W-DIAG: the value-free conversion reason code reaches every conversion-error envelope.

``conversion_failure_context`` is the single adapter every conversion-error envelope
routes through. It always carries the backwards-compatible generic ``conversion_code``
and, only when the wrapped cfxmark error exposes a ``reason_code`` in cfxmark's
published allowlist, the specific ``conversion_reason_code``. The exception message
string is never parsed and no leaf value ever crosses the boundary.

Coverage:
* adapter projection contract (value-free, backwards compatible, allowlist-gated);
* the four rerouted surfaces (stateless create, stateless update, managed preflight,
  page inspect) each carry the reason code when a coded error is injected, and each
  envelope stays value-free (asserted on the structured dict, not a raw string grep);
* a real Markdown body that trips ``semantic-mapping-ambiguous`` end-to-end through
  the two tie-reaching paths (managed preflight + stateless update);
* fallback: a non-allowlisted or absent reason code yields an envelope WITHOUT
  ``conversion_reason_code`` while keeping the generic ``conversion_code``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cfxmark
import pytest

from atlassian_skills.confluence.migration_preflight import (
    build_managed_preflight,
    conversion_failure_context,
)
from atlassian_skills.confluence.page_inspect import inspect_page
from atlassian_skills.confluence.pull_md import pull_md
from atlassian_skills.confluence.stateless_write import (
    build_page_update_preflight,
    build_source_conversion,
)
from atlassian_skills.core.errors import AtlasError
from tests.unit.test_stateless_page_write import FakeClient as StatelessClient

REASON = "semantic-mapping-ambiguous"

# A base with duplicate sibling paragraphs; deleting one is a minimum-cost tie the
# semantic matcher refuses to resolve, so managed validation fails closed with
# semantic-mapping-ambiguous. Used for the real end-to-end tie paths.
TIE_STORAGE = "<p>A</p><p>X</p><p>X</p><p>B</p>"
TIE_EDITED_MD = "A\n\nX\n\nB\n"

# Distinctive markers planted in the injected exception message + a page-content
# fragment. Neither may appear anywhere in a serialized envelope: their absence
# proves the adapter never parses str(error) nor carries leaf content.
MSG_MARKER = "CONVERSIONMSGLEAKMARKER"
CONTENT_MARKER = "PAGECONTENTLEAKMARKER"

# The only keys a conversion-error context may carry.
_SAFE_CONVERSION_CONTEXT_KEYS = frozenset({"reason", "conversion_code", "conversion_reason_code"})


class ManagedClient:
    """Minimal managed client: serves a fixed storage body, no attachments."""

    base_url = "https://example.com/confluence"

    def __init__(self, storage: str) -> None:
        self.storage = storage
        self.version = 7

    def get_page(self, page_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=page_id,
            title="Page",
            body_storage=self.storage,
            version=SimpleNamespace(number=self.version),
        )

    def list_attachments(self, page_id: str) -> list[Any]:
        return []


def _raiser(exc: BaseException) -> Any:
    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    return _raise


def _coded_error() -> cfxmark.ConversionError:
    return cfxmark.ConversionError(f"{MSG_MARKER} <p>{CONTENT_MARKER}</p>", reason_code=REASON)


def _leaf_strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _leaf_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _leaf_strings(value)


# ---------------------------------------------------------------------------
# Injection surfaces: force each rerouted error path with a coded ConversionError.
# ---------------------------------------------------------------------------


def _inject_stateless_create(_tmp: Path, mp: pytest.MonkeyPatch) -> AtlasError:
    mp.setattr(cfxmark, "to_cfx_artifact", _raiser(_coded_error()))
    with pytest.raises(AtlasError) as info:
        build_source_conversion("# clean heading\n\nBody paragraph.\n")
    return info.value


def _inject_stateless_update(_tmp: Path, mp: pytest.MonkeyPatch) -> AtlasError:
    client = StatelessClient(TIE_STORAGE)
    mp.setattr(cfxmark, "validate_managed_cfx_artifact", _raiser(_coded_error()))
    with pytest.raises(AtlasError) as info:
        build_page_update_preflight(client, "123", TIE_EDITED_MD, body_format="md", if_version=7)
    return info.value


def _inject_managed_preflight(tmp: Path, mp: pytest.MonkeyPatch) -> AtlasError:
    client = ManagedClient(TIE_STORAGE)
    path = tmp / "page.md"
    pull_md(client, "123", output_path=path, portable=True, no_assets=True)
    path.write_text(path.read_text(encoding="utf-8").replace("X\n\nX", "X", 1), encoding="utf-8")
    mp.setattr(cfxmark, "validate_managed_cfx_artifact", _raiser(_coded_error()))
    with pytest.raises(AtlasError) as info:
        build_managed_preflight(client, "123", path)
    return info.value


def _inject_page_inspect(_tmp: Path, mp: pytest.MonkeyPatch) -> AtlasError:
    client = ManagedClient("<p>x</p>")
    mp.setattr(cfxmark, "to_md_artifact", _raiser(_coded_error()))
    with pytest.raises(AtlasError) as info:
        inspect_page(client, "123", intent="read")
    return info.value


_INJECTION_SURFACES = {
    "stateless_create": _inject_stateless_create,
    "stateless_update": _inject_stateless_update,
    "managed_preflight": _inject_managed_preflight,
    "page_inspect": _inject_page_inspect,
}


# ---------------------------------------------------------------------------
# Adapter projection contract.
# ---------------------------------------------------------------------------


def test_adapter_projection_is_allowlist_gated_and_backwards_compatible() -> None:
    # Allowlisted code → generic code plus the specific reason code.
    assert conversion_failure_context(cfxmark.ConversionError("m", reason_code=REASON)) == {
        "conversion_code": "conversion_error",
        "conversion_reason_code": REASON,
    }
    # Non-allowlisted code → dropped; the generic code is untouched.
    assert conversion_failure_context(cfxmark.ConversionError("m", reason_code="not-in-allowlist")) == {
        "conversion_code": "conversion_error"
    }
    # No structured attribute at all → generic-by-type code only (message never read).
    assert conversion_failure_context(cfxmark.ParseError("m")) == {"conversion_code": "parse_error"}
    assert conversion_failure_context(ValueError("m")) == {"conversion_code": "invalid_input"}


# ---------------------------------------------------------------------------
# Per-surface: reason code present AND envelope value-free (structured assertions).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", list(_INJECTION_SURFACES))
def test_injected_reason_code_reaches_every_surface_value_free(
    surface: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = _INJECTION_SURFACES[surface](tmp_path, monkeypatch)
    envelope = error.to_dict()
    context = envelope["error"]["context"]

    # The specific value-free code survives, consistently across all four surfaces.
    assert context.get("conversion_reason_code") == REASON, surface
    # The backwards-compatible generic code is still present.
    assert context.get("conversion_code"), surface
    # Deny by default: no context key outside the conversion-envelope allowlist.
    extra = set(context) - _SAFE_CONVERSION_CONTEXT_KEYS
    assert not extra, f"{surface}: non-allowlisted context keys {sorted(extra)}"
    # Value-free: neither the exception message nor page content leaks into any leaf.
    for leaf in _leaf_strings(envelope):
        assert MSG_MARKER not in leaf, f"{surface}: leaked exception message"
        assert CONTENT_MARKER not in leaf, f"{surface}: leaked page content"


# ---------------------------------------------------------------------------
# Real tie end-to-end (the two tie-reaching paths).
# ---------------------------------------------------------------------------


def test_tie_end_to_end_stateless_update() -> None:
    client = StatelessClient(TIE_STORAGE)
    with pytest.raises(AtlasError) as info:
        build_page_update_preflight(client, "123", TIE_EDITED_MD, body_format="md", if_version=7)
    context = info.value.to_dict()["error"]["context"]
    assert context["conversion_reason_code"] == REASON
    assert context["conversion_code"] == "conversion_error"


def test_tie_end_to_end_managed_preflight(tmp_path: Path) -> None:
    client = ManagedClient(TIE_STORAGE)
    path = tmp_path / "page.md"
    pull_md(client, "123", output_path=path, portable=True, no_assets=True)
    path.write_text(path.read_text(encoding="utf-8").replace("X\n\nX", "X", 1), encoding="utf-8")
    with pytest.raises(AtlasError) as info:
        build_managed_preflight(client, "123", path)
    context = info.value.to_dict()["error"]["context"]
    assert context["conversion_reason_code"] == REASON
    assert context["conversion_code"] == "conversion_error"


# ---------------------------------------------------------------------------
# Fallback: a non-allowlisted or absent reason code is omitted, not passed through.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason_code", ["not-in-allowlist", None])
def test_out_of_allowlist_reason_code_is_omitted(reason_code: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfxmark, "to_cfx_artifact", _raiser(cfxmark.ConversionError("m", reason_code=reason_code)))
    with pytest.raises(AtlasError) as info:
        build_source_conversion("# clean heading\n\nBody paragraph.\n")
    context = info.value.to_dict()["error"]["context"]
    assert "conversion_reason_code" not in context
    # The generic code path stays intact (backwards compatible).
    assert context["conversion_code"] == "conversion_error"
