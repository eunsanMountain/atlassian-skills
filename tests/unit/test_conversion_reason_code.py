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

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cfxmark
import pytest

from atlassian_skills.confluence.migration_preflight import (
    build_managed_preflight,
    conversion_failure_context,
    describe_migration_code,
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
#
# `conversion_reason_description` and `supported_alternatives` are atls-authored
# constants selected by a stable code -- a sentence from this repository's own
# table and a fixed list of two command shapes. They are on this list because a
# caller that gets only codes cannot act: a live publish refused with
# `semantic-mapping-ambiguous` and had nothing to print but the code.
#
# The marker assertions below still apply to them, so if either ever starts
# carrying cfxmark's message or page content, this file fails.
_SAFE_CONVERSION_CONTEXT_KEYS = frozenset(
    {
        "reason",
        "conversion_code",
        "conversion_reason_code",
        "conversion_reason_description",
        "supported_alternatives",
        # Counts and code names only -- asserted separately below to carry no
        # page text, by the same marker check as everything else here.
        "regeneration_outlook",
    }
)


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
    # Allowlisted code → generic code, the specific reason code, and the two
    # static things that make it actionable. A live publish refused with
    # `ownership_proof_invalid/semantic-mapping-ambiguous` and its caller had
    # nothing to print but those two words; the sentence and the alternatives
    # existed in atls and never crossed the boundary.
    #
    # Both additions are atls-authored constants keyed by a stable code -- never
    # cfxmark's message or display_label -- which is what the rest of this file
    # exists to keep true.
    assert conversion_failure_context(cfxmark.ConversionError("m", reason_code=REASON)) == {
        "conversion_code": "conversion_error",
        "conversion_reason_code": REASON,
        "conversion_reason_description": describe_migration_code(REASON),
        "supported_alternatives": ["append_markdown_blocks", "page_patch_text"],
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
    """A tie now arrives as a proof refusal rather than a conversion failure.

    The converter's validator repeats the generator's base-free fallback instead
    of raising at it, so the candidate is reconstructable and the refusal comes
    from the ownership proof. The caller gets the diagnosis -- fatal class,
    counts, the diagnostics and their resolutions -- where it used to get two
    bare codes.
    """

    client = StatelessClient(TIE_STORAGE)
    with pytest.raises(AtlasError) as info:
        build_page_update_preflight(client, "123", TIE_EDITED_MD, body_format="md", if_version=7)
    context = info.value.to_dict()["error"]["context"]
    assert context["reason"] == "ownership_proof_invalid"
    assert context["fatal_class"]
    # The cause still names itself in a value-free machine code, which is what
    # the old `conversion_reason_code` assertion protected.
    assert REASON in {item["code"] for item in context["diagnostics"]}


def test_tie_end_to_end_managed_preflight(tmp_path: Path) -> None:
    """And on the managed path the loss gate now gets to answer.

    It could not before: the converter raised where the gate would have run, so
    a tie was refused without anyone asking what the candidate actually costs.
    This edit deletes one of two identical paragraphs and loses nothing, the
    candidate agrees with an independent regeneration, and the publish proceeds
    with the proof waived rather than the proof missing.

    The gate is not "zero migrations". It requires the candidate to lose nothing
    named, to be classifiable, and to match a regeneration -- so this asserts the
    waiver is recorded, not merely that no exception escaped.
    """

    client = ManagedClient(TIE_STORAGE)
    path = tmp_path / "page.md"
    pull_md(client, "123", output_path=path, portable=True, no_assets=True)
    path.write_text(path.read_text(encoding="utf-8").replace("X\n\nX", "X", 1), encoding="utf-8")

    preflight = build_managed_preflight(client, "123", path)
    assert preflight.ownership["proof_waived"] is True
    assert preflight.proof_mode == "regeneration_verified"
    assert preflight.to_dict()["candidate_loss"]["named_losses"] == []


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


# ---------------------------------------------------------------------------
# The other reading, when the proof cannot produce one
# ---------------------------------------------------------------------------


def test_an_ambiguous_proof_reports_what_regenerating_would_cost() -> None:
    """The refusal used to be a dead end.

    The proof aligns the stored page against the edited Markdown, and when that
    alignment has two equally cheap readings it raises before producing a
    candidate -- so there is nothing to gate on and nothing to decide. Measured
    on a real document: a live Story page that Markdown holds completely could not be
    published and the error said only `semantic-mapping-ambiguous`.

    The refusal now also says what publishing from the Markdown alone would drop,
    which is the number a caller needs before asking for that instead. It still
    publishes nothing.
    """

    from atlassian_skills.confluence.stateless_write import _regeneration_outlook

    options = cfxmark.ConversionOptions(profile="editable")
    remote = (
        "<p>alpha</p>"
        '<ac:structured-macro ac:name="info" ac:macro-id="7f3a">'
        "<ac:rich-text-body><p>note body</p></ac:rich-text-body></ac:structured-macro>"
    )
    # The macro survives the edit -- it is still in the Markdown. What a
    # regeneration cannot carry is the id the server gave it, which is the whole
    # shape this outlook exists to price. Deleting the macro instead would be a
    # deliberate removal and not an identity loss at all.
    edited = cfxmark.to_md_artifact(remote, options=options).markdown.replace("alpha", "alpha edited")
    outlook = _regeneration_outlook(remote, edited, options)

    reading = outlook["regeneration_outlook"]
    # It describes; it does not permit. Both of these were read as permission
    # once already, in a report to the owner, before they were named this way.
    assert reading["diagnostic_only"] is True
    assert reading["safe_to_publish"] is False
    assert set(reading) == {
        "diagnostic_only",
        "safe_to_publish",
        "blocking_reasons",
        "named_losses",
        "identity",
        "named_loss_approval_required",
    }
    # Counts and code names, whatever the document. Values depend on it -- this
    # fixture's editable Markdown binds the macro id and loses nothing, while the
    # live Story page that prompted this carries a portable macro fence and loses one.
    assert isinstance(reading["named_losses"], list)
    assert isinstance(reading["identity"], list)
    assert isinstance(reading["named_loss_approval_required"], bool)


def test_identity_the_regeneration_cannot_carry_is_a_blocking_reason() -> None:
    """`named_loss_approval_required` counts named losses only. A macro id the
    server issued is refused elsewhere and is nobody's to approve, so `false`
    there means "nothing to ask about" and never "nothing to lose"."""

    from atlassian_skills.confluence.stateless_write import _regeneration_outlook

    options = cfxmark.ConversionOptions(profile="editable")
    remote = (
        "<p>alpha</p>"
        '<ac:structured-macro ac:name="info" ac:macro-id="7f3a">'
        "<ac:rich-text-body><p>note body</p></ac:rich-text-body></ac:structured-macro>"
    )
    # A portable fence: the macro survives the edit and its id has nothing to
    # bind to, which is the shape that live Story page is in.
    portable = cfxmark.to_md_artifact(remote, options=cfxmark.ConversionOptions(profile="readable")).markdown
    reading = _regeneration_outlook(remote, portable, options)["regeneration_outlook"]

    if reading["identity"]:
        assert "identity_not_carried" in reading["blocking_reasons"]
    assert reading["safe_to_publish"] is False


def test_the_outlook_is_absent_rather_than_wrong_when_it_cannot_be_computed() -> None:
    """A regeneration that also fails must leave the refusal as it was. Replacing
    one opaque error with two helps nobody."""

    from atlassian_skills.confluence.stateless_write import _regeneration_outlook

    assert _regeneration_outlook("<p>a</p>", "x", object()) == {}


def test_the_outlook_never_carries_page_text() -> None:
    """Same boundary as the rest of this file: a finding code is a name we chose
    and a count is an integer; neither says what the page says."""

    from atlassian_skills.confluence.stateless_write import _regeneration_outlook

    remote = f"<p>{CONTENT_MARKER}</p>"
    outlook = _regeneration_outlook(remote, f"{CONTENT_MARKER} edited\n", cfxmark.ConversionOptions(profile="editable"))
    assert CONTENT_MARKER not in json.dumps(outlook)
