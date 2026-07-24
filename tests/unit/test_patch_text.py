from __future__ import annotations

from types import SimpleNamespace

import pytest

from atlassian_skills.confluence.patch_text import (
    PatchSelector,
    build_patch_candidate,
    node_fingerprint,
    parse_patch_document,
    patch_text,
)
from atlassian_skills.core.errors import StaleError, ValidationError


class FakeClient:
    def __init__(self, storage: str, *, version: int = 7) -> None:
        self.storage = storage
        self.version = version
        self.update_calls: list[tuple[str, str, str, int, str, str | None, bool]] = []
        self.apply_update = True
        self.raise_before_update = False
        self.raise_after_update = False
        self.server_storage_after_update: str | None = None
        self.get_calls = 0
        self.mutate_on_get: int | None = None

    def get_page(self, page_id: str) -> SimpleNamespace:
        self.get_calls += 1
        if self.mutate_on_get == self.get_calls:
            self.storage = "<p>Remote changed</p>"
            self.version += 1
        return SimpleNamespace(
            id=page_id,
            title="Synthetic Page",
            body_storage=self.storage,
            version=SimpleNamespace(number=self.version),
        )

    def update_page(
        self,
        page_id: str,
        title: str,
        body: str,
        version_number: int,
        body_format: str = "storage",
        *,
        reason: str | None = None,
        minor_edit: bool = False,
    ) -> dict[str, object]:
        if self.raise_before_update:
            raise RuntimeError("connection failed before send")
        assert version_number == self.version + 1
        self.update_calls.append((page_id, title, body, version_number, body_format, reason, minor_edit))
        if self.apply_update:
            self.storage = self.server_storage_after_update or body
            self.version = version_number
        if self.raise_after_update:
            raise RuntimeError("response lost after send")
        return {"id": page_id, "version": {"number": version_number}}


def test_patch_text_replaces_exactly_one_plain_text_leaf_and_readbacks() -> None:
    client = FakeClient('<p>Date: 2026-07-16 <strong>stable</strong></p><p data-note="2026-07-16">Other</p>')

    result = patch_text(client, "123456", old="2026-07-16", new="2026-07-17", if_version=7)

    assert result["status"] == "updated"
    assert result["version"] == 8
    assert result["node_path"].endswith("/p[1]/text()[1]")
    assert client.storage == '<p>Date: 2026-07-17 <strong>stable</strong></p><p data-note="2026-07-16">Other</p>'


def test_patch_text_dry_run_reports_exact_node_without_put() -> None:
    client = FakeClient("<p>before 123 after</p>")

    result = patch_text(client, "123456", old="123", new="124", if_version=7, dry_run=True)

    assert {key: value for key, value in result.items() if key != "changes"} == {
        "status": "dry_run",
        "patchable": True,
        "match_count": 1,
        "page_id": "123456",
        "version": 7,
        "node_path": "/root[1]/p[1]/text()[1]",
        "before": "123",
        "after": "124",
        "put_count": 0,
    }
    assert result["changes"] == [
        {
            "node_path": "/root[1]/p[1]/text()[1]",
            "before_fingerprint": node_fingerprint("/root[1]/p[1]/text()[1]", "before 123 after"),
            "before_text": "before 123 after",
            "after_text": "before 124 after",
        }
    ]
    assert client.update_calls == []


def test_patch_text_does_not_trust_put_response_without_independent_readback() -> None:
    client = FakeClient("<p>before 123 after</p>")
    client.apply_update = False

    with pytest.raises(ValidationError) as exc_info:
        patch_text(client, "123456", old="123", new="124", if_version=7)

    assert exc_info.value.context == {"page_id": "123456", "reason": "readback_mismatch"}
    assert client.storage == "<p>before 123 after</p>"
    assert client.version == 7


def test_stateless_patch_response_loss_is_adopted_and_retry_is_already_applied() -> None:
    client = FakeClient("<p>Alpha</p>")
    client.raise_after_update = True

    first = patch_text(client, "123456", old="Alpha", new="Beta", if_version=7)

    assert first["status"] == "updated"
    assert first["recovery"] == "lost_response_adopted"
    assert len(client.update_calls) == 1
    client.raise_after_update = False

    second = patch_text(client, "123456", old="Alpha", new="Beta", if_version=7)

    assert second["status"] == "already_applied"
    assert second["recovery"] == "before_after_selector_matched"
    assert second["put_count"] == 0
    assert len(client.update_calls) == 1


def test_stateless_patch_unapplied_response_loss_is_retryable_without_duplicate_guess() -> None:
    client = FakeClient("<p>Alpha</p>")
    client.raise_before_update = True

    with pytest.raises(ValidationError) as exc_info:
        patch_text(client, "123456", old="Alpha", new="Beta", if_version=7)

    assert exc_info.value.context["reason"] == "patch_put_failed"
    assert client.storage == "<p>Alpha</p>"
    client.raise_before_update = False

    result = patch_text(client, "123456", old="Alpha", new="Beta", if_version=7)

    assert result["status"] == "updated"
    assert len(client.update_calls) == 1


def test_stateless_patch_second_remote_check_blocks_drift() -> None:
    client = FakeClient("<p>Alpha</p>")
    client.mutate_on_get = 2

    with pytest.raises(StaleError) as exc_info:
        patch_text(client, "123456", old="Alpha", new="Beta", if_version=7)

    assert exc_info.value.context["reason"] == "prewrite_remote_drift"
    assert client.update_calls == []


@pytest.mark.parametrize(
    "storage",
    [
        "<p>same</p><p>same</p>",
        '<p data-note="same">other</p>',
        '<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[same]]></ac:plain-text-body></ac:structured-macro>',
        '<ac:structured-macro ac:name="x"><ac:parameter ac:name="p">same</ac:parameter></ac:structured-macro>',
        "<p>sa<strong>me</strong></p>",
    ],
)
def test_patch_text_rejects_multiple_attribute_macro_code_and_boundary_matches(storage: str) -> None:
    client = FakeClient(storage)

    with pytest.raises(ValidationError):
        patch_text(client, "123456", old="same", new="changed")

    assert client.update_calls == []


def test_patch_text_stale_version_is_exit_5_error() -> None:
    client = FakeClient("<p>same</p>", version=8)

    with pytest.raises(StaleError):
        patch_text(client, "123456", old="same", new="changed", if_version=7)

    assert client.update_calls == []


def test_patch_text_escapes_replacement_without_touching_other_storage() -> None:
    client = FakeClient("<p>A &amp; B</p><p>Keep &lt;x&gt;</p>")

    patch_text(client, "123456", old="A & B", new="A < B")

    assert client.storage == "<p>A &lt; B</p><p>Keep &lt;x&gt;</p>"


def test_patch_document_requires_version_and_exact_selector_schema() -> None:
    selector = PatchSelector(
        node_path="/root[1]/p[1]/text()[1]",
        before_fingerprint=node_fingerprint("/root[1]/p[1]/text()[1]", "Alpha"),
        before_text="Alpha",
        after_text="Beta",
    )

    document = parse_patch_document(
        {
            "version": 7,
            "changes": [
                {
                    "node_path": selector.node_path,
                    "before_fingerprint": selector.before_fingerprint,
                    "before_text": selector.before_text,
                    "after_text": selector.after_text,
                }
            ],
        }
    )

    assert document.version == 7
    assert document.changes == (selector,)


def test_batch_patch_is_one_candidate_and_rejects_duplicate_or_overlap_before_put() -> None:
    storage = "<p>Alpha</p><p>Bravo</p>"
    first = PatchSelector(
        node_path="/root[1]/p[1]/text()[1]",
        before_fingerprint=node_fingerprint("/root[1]/p[1]/text()[1]", "Alpha"),
        before_text="Alpha",
        after_text="Changed A",
    )
    second = PatchSelector(
        node_path="/root[1]/p[2]/text()[1]",
        before_fingerprint=node_fingerprint("/root[1]/p[2]/text()[1]", "Bravo"),
        before_text="Bravo",
        after_text="Changed B",
    )

    candidate = build_patch_candidate(storage, (first, second))

    assert candidate.storage == "<p>Changed A</p><p>Changed B</p>"
    assert [item.node_path for item in candidate.changes] == [first.node_path, second.node_path]
    with pytest.raises(ValidationError) as exc_info:
        build_patch_candidate(storage, (first, first))
    assert exc_info.value.context["reason"] == "duplicate_or_overlapping_selector"


def test_batch_patch_rejects_wrong_path_or_fingerprint_without_put() -> None:
    storage = "<p>Alpha</p>"
    selector = PatchSelector(
        node_path="/root[1]/p[1]/text()[1]",
        before_fingerprint=node_fingerprint("/root[1]/p[1]/text()[1]", "Different"),
        before_text="Alpha",
        after_text="Beta",
    )

    with pytest.raises(ValidationError) as exc_info:
        build_patch_candidate(storage, (selector,))

    assert exc_info.value.context == {
        "reason": "selector_fingerprint_mismatch",
        "node_path": selector.node_path,
    }


def test_patch_text_sends_reason_and_minor_edit_in_one_put() -> None:
    client = FakeClient("<p>Alpha</p>")

    patch_text(
        client,
        "123456",
        old="Alpha",
        new="Beta",
        reason="Correct release date",
        minor_edit=True,
    )

    assert len(client.update_calls) == 1
    assert client.update_calls[0][-2:] == ("Correct release date", True)


# ---------------------------------------------------------------------------
# Story 4.5: --find failure classification (plan section 5.8)
#
# Before this, every zero-match failure collapsed into
# text_occurrence_not_unique, so a page where the text simply did not exist,
# and a page where it spanned inline markup, both told the caller the match was
# ambiguous while reporting match_count=0. That is the opposite of the truth
# and sends an agent looking for duplicates that are not there.
# ---------------------------------------------------------------------------

_MACRO_PARAM = "<ac:structured-macro><ac:parameter>Zeta</ac:parameter></ac:structured-macro>"
_MACRO_BODY = "<ac:structured-macro><ac:plain-text-body><![CDATA[Kappa]]></ac:plain-text-body></ac:structured-macro>"


@pytest.mark.parametrize(
    ("label", "storage", "find", "reason", "counts"),
    [
        # Two or more eligible occurrences win the precedence outright.
        (
            "duplicate across blocks",
            "<p>Alpha</p><p>Alpha</p>",
            "Alpha",
            "text_occurrence_not_unique",
            {"match_count": 2},
        ),
        (
            "duplicate inside one leaf",
            "<p>Alpha and Alpha</p>",
            "Alpha",
            "text_occurrence_not_unique",
            {"match_count": 2},
        ),
        # Adjacent visible leaves inside one block: the text is real but no
        # single leaf holds it.
        (
            "inline strong boundary",
            "<p><strong>Important</strong> notice</p>",
            "Important notice",
            "cross_text_node_boundary",
            {"match_count": 0, "boundary_match_count": 1},
        ),
        (
            "inline link boundary",
            '<p>see <a href="https://example.test/x">docs</a> now</p>',
            "docs now",
            "cross_text_node_boundary",
            {"match_count": 0, "boundary_match_count": 1},
        ),
        (
            "inline code boundary",
            "<p>run <code>atls</code> here</p>",
            "atls here",
            "cross_text_node_boundary",
            {"match_count": 0, "boundary_match_count": 1},
        ),
        # Ineligible containers: the text exists but cannot be patched safely.
        (
            "macro parameter",
            _MACRO_PARAM,
            "Zeta",
            "unsupported_target_context",
            {"match_count": 0, "excluded_match_count": 1},
        ),
        (
            "macro plain-text body",
            _MACRO_BODY,
            "Kappa",
            "unsupported_target_context",
            {"match_count": 0, "excluded_match_count": 1},
        ),
        (
            "attribute value",
            '<p><a href="https://example.test/visit now">link</a></p>',
            "visit now",
            "unsupported_target_context",
            {"match_count": 0, "excluded_match_count": 1},
        ),
        # Genuinely absent.
        (
            "absent",
            "<p>Alpha</p>",
            "Omega",
            "text_not_found",
            {"match_count": 0, "boundary_match_count": 0, "excluded_match_count": 0},
        ),
        # Runs must not be joined across block or structural boundaries.
        (
            "across sibling paragraphs",
            "<p>first</p><p>second</p>",
            "firstsecond",
            "text_not_found",
            {"boundary_match_count": 0},
        ),
        (
            "across a hard break",
            "<p>left<br/>right</p>",
            "leftright",
            "text_not_found",
            {"boundary_match_count": 0},
        ),
        (
            "across table cells",
            "<table><tbody><tr><td>a</td><td>b</td></tr></tbody></table>",
            "ab",
            "text_not_found",
            {"boundary_match_count": 0},
        ),
        (
            "across list items",
            "<ul><li>a</li><li>b</li></ul>",
            "ab",
            "text_not_found",
            {"boundary_match_count": 0},
        ),
        (
            "across a nested block",
            "<blockquote><p>inner</p></blockquote><p>outer</p>",
            "innerouter",
            "text_not_found",
            {"boundary_match_count": 0},
        ),
    ],
)
def test_find_failure_reason_follows_precedence(
    label: str, storage: str, find: str, reason: str, counts: dict[str, int]
) -> None:
    client = FakeClient(storage)

    with pytest.raises(ValidationError) as exc_info:
        patch_text(client, "123456", old=find, new="REPLACED", dry_run=True)

    context = exc_info.value.context
    assert context["reason"] == reason, f"{label}: {context}"
    assert context["patchable"] is False, label
    for key, expected in counts.items():
        assert context[key] == expected, f"{label}: {key} -> {context}"
    assert client.update_calls == [], f"{label}: failure must not write"


def test_find_failure_reports_a_constant_hint_code() -> None:
    client = FakeClient("<p><strong>Important</strong> notice</p>")

    with pytest.raises(ValidationError) as exc_info:
        patch_text(client, "123456", old="Important notice", new="REPLACED", dry_run=True)

    context = exc_info.value.context
    assert context["hint_code"] == "use_single_plain_text_leaf"
    assert isinstance(exc_info.value.hint, str) and exc_info.value.hint


def test_find_failure_offers_safe_next_actions_without_argv() -> None:
    """Plan section 11.1: patch actions never synthesize a --find from server text."""
    client = FakeClient("<p><strong>Important</strong> notice</p>")

    with pytest.raises(ValidationError) as exc_info:
        patch_text(client, "123456", old="Important notice", new="REPLACED", dry_run=True)

    actions = exc_info.value.context["next_actions"]
    assert [action["id"] for action in actions] == ["retry_inner_plain_text", "use_pull_md"]
    for action in actions:
        assert "argv" not in action, "patch next actions must not carry executable argv"
        assert action["requires_user_approval"] is False
        assert action["description_code"].isupper()


def test_ambiguous_match_outranks_a_boundary_match() -> None:
    """Precedence is fixed, not first-match: duplicates are reported before boundaries."""
    storage = "<p>Alpha</p><p><strong>Alpha</strong> tail</p>"
    client = FakeClient(storage)

    with pytest.raises(ValidationError) as exc_info:
        patch_text(client, "123456", old="Alpha", new="REPLACED", dry_run=True)

    assert exc_info.value.context["reason"] == "text_occurrence_not_unique"
    assert exc_info.value.context["match_count"] == 2


def test_single_eligible_leaf_still_patches() -> None:
    client = FakeClient("<p><strong>Important</strong> notice</p>")

    result = patch_text(client, "123456", old="Important", new="Critical", dry_run=True)

    assert result["patchable"] is True
    assert result["put_count"] == 0


def test_bundled_skill_routes_each_patch_failure_without_escalating_to_migration() -> None:
    """The skill must not answer a patch failure by proposing a lossy rewrite.

    An agent that reads "patch failed" and jumps to pull-md turns a two-character
    correction into a whole-page migration, which is exactly the loss the
    diagnostics exist to avoid.
    """
    from pathlib import Path

    skill = (
        Path(__file__).resolve().parents[2] / "src" / "atlassian_skills" / "_assets" / "skills" / "atls" / "SKILL.md"
    ).read_text(encoding="utf-8")

    for reason in (
        "text_occurrence_not_unique",
        "cross_text_node_boundary",
        "unsupported_target_context",
        "text_not_found",
    ):
        assert reason in skill, f"SKILL.md does not route {reason}"

    assert "not a reason to switch to a lossy full-page migration" in skill
    assert "Never synthesize a new `--find` value from server text" in skill


# ---------------------------------------------------------------------------
# Story 4.5 closure: diagnostic segments must not affect patch selection
# ---------------------------------------------------------------------------


def test_repeated_code_macros_do_not_block_an_unrelated_patch() -> None:
    """CDATA is recorded for diagnostics only; it must not claim selector identity.

    Two code macros under one parent share a synthetic .../cdata() path. When
    those segments were registered in the selector identity map, any page with
    more than one code block rejected every patch on it, including paragraphs
    that have nothing to do with the macro.
    """
    storage = (
        "<ac:structured-macro><ac:plain-text-body>"
        "<![CDATA[first]]><!--split--><![CDATA[second]]>"
        "</ac:plain-text-body></ac:structured-macro>"
        "<p>Old</p>"
    )
    client = FakeClient(storage)

    result = patch_text(client, "123456", old="Old", new="New", dry_run=True)

    assert result["patchable"] is True
    assert result["node_path"] == "/root[1]/p[1]/text()[1]"


def test_repeated_attributes_do_not_block_an_unrelated_patch() -> None:
    storage = '<p><a href="https://example.test/a">one</a><a href="https://example.test/b">two</a></p><p>Old</p>'
    client = FakeClient(storage)

    result = patch_text(client, "123456", old="Old", new="New", dry_run=True)

    assert result["patchable"] is True


def test_code_macro_text_is_still_reported_as_an_unsupported_target() -> None:
    """Excluding CDATA from selection must not silence its diagnostic value."""
    storage = "<ac:structured-macro><ac:plain-text-body><![CDATA[Kappa]]></ac:plain-text-body></ac:structured-macro>"
    client = FakeClient(storage)

    with pytest.raises(ValidationError) as exc_info:
        patch_text(client, "123456", old="Kappa", new="X", dry_run=True)

    assert exc_info.value.context["reason"] == "unsupported_target_context"
    assert exc_info.value.context["excluded_match_count"] == 1


# ---------------------------------------------------------------------------
# Story 4.5 closure: Unicode NFC comparison (plan section 5.8)
# ---------------------------------------------------------------------------


def test_find_matches_across_unicode_normalization_forms() -> None:
    """Storage may hold NFD while the user's --find is NFC, or the reverse.

    Text copied from macOS is frequently NFD; the same visible word typed
    elsewhere is NFC. Comparing raw code points reports text_not_found for text
    the user can plainly see on the page.
    """
    import unicodedata

    decomposed = unicodedata.normalize("NFD", "Café")
    composed = unicodedata.normalize("NFC", "Café")
    assert decomposed != composed

    client = FakeClient(f"<p>{decomposed} menu</p>")
    result = patch_text(client, "123456", old=composed, new="Bistro", dry_run=True)

    assert result["patchable"] is True


def test_normalization_fallback_replaces_only_the_matched_span() -> None:
    """The rest of the leaf keeps its original bytes, including its own form."""
    import unicodedata

    decomposed_cafe = unicodedata.normalize("NFD", "Café")
    decomposed_tail = unicodedata.normalize("NFD", "naïve")
    storage = f"<p>{decomposed_cafe} and {decomposed_tail}</p>"
    client = FakeClient(storage)

    result = patch_text(
        client,
        "123456",
        old=unicodedata.normalize("NFC", "Café"),
        new="Bistro",
        dry_run=True,
    )

    assert result["patchable"] is True
    # The untouched tail must not be silently re-normalized: only the matched
    # span is spliced, so the rest of the leaf keeps its original bytes.
    assert result["changes"][0]["after_text"] == f"Bistro and {decomposed_tail}"


def test_exact_matches_win_over_normalized_ones() -> None:
    """A page that already matches exactly must not change behaviour."""
    client = FakeClient("<p>Alpha and Alpha again</p>")

    result = patch_text(client, "123456", old="Alpha and", new="Beta and", dry_run=True)

    assert result["patchable"] is True
    assert result["changes"][0]["after_text"] == "Beta and Alpha again"


def test_normalized_matching_covers_hangul_composition() -> None:
    """The opener filter must not assume Latin combining marks.

    Hangul decomposes into jamo, so the first character of the decomposed form
    differs from the composed syllable entirely. A filter tuned only for accents
    would skip these offsets and report text_not_found.
    """
    import unicodedata

    decomposed = unicodedata.normalize("NFD", "한글")
    composed = unicodedata.normalize("NFC", "한글")
    assert decomposed != composed

    client = FakeClient(f"<p>{decomposed} 문서</p>")
    result = patch_text(client, "123456", old=composed, new="영문", dry_run=True)

    assert result["patchable"] is True
    assert result["changes"][0]["after_text"] == "영문 문서"
