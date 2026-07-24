"""Exact-append eligibility must not depend on control-comment position.

Failure mode: an EOF append lost exact-append eligibility (and then failed
the full-migration proof) only because the pull
re-emits control comments — cfxmark:align / multi-line cfxmark:payloads
sidecars / migration blocks — at normalized positions, so the byte-equality
projection check saw the appended paragraph on the wrong side of a comment.

The projection check now compares a position-invariant view: the exact same
comment/payload-section multiset AND byte-identical remaining content.
Any real content difference still disqualifies the append.
"""

from __future__ import annotations

from atlassian_skills.confluence.migration_preflight import _append_comment_position_view


def test_comment_position_is_the_only_freedom() -> None:
    body = "alpha paragraph\n\n<!-- cfxmark:align=left -->\n\nbeta paragraph\n"
    moved = "alpha paragraph\n\nbeta paragraph\n\n<!-- cfxmark:align=left -->\n"
    assert _append_comment_position_view(body) == _append_comment_position_view(moved)


def test_payload_section_moves_as_one_unit() -> None:
    section = '<!-- cfxmark:payloads -->\n<!-- op-abc123\n<time datetime="2026-01-01"/> -->\n<!-- /cfxmark:payloads -->'
    before = f"alpha paragraph\n\n{section}\n\nappended text\n"
    after = f"alpha paragraph\n\nappended text\n\n{section}\n"
    assert _append_comment_position_view(before) == _append_comment_position_view(after)


def test_content_difference_still_disqualifies() -> None:
    a = "alpha paragraph\n\n<!-- cfxmark:align=left -->\n"
    b = "alpha paragraph changed\n\n<!-- cfxmark:align=left -->\n"
    assert _append_comment_position_view(a) != _append_comment_position_view(b)


def test_comment_multiset_difference_still_disqualifies() -> None:
    a = "alpha paragraph\n\n<!-- cfxmark:align=left -->\n"
    b = "alpha paragraph\n\n<!-- cfxmark:align=right -->\n"
    c = "alpha paragraph\n"
    assert _append_comment_position_view(a) != _append_comment_position_view(b)
    assert _append_comment_position_view(a) != _append_comment_position_view(c)


def test_payload_entry_difference_still_disqualifies() -> None:
    one = "alpha\n\n<!-- cfxmark:payloads -->\n<!-- op-abc123\n<time/> -->\n<!-- /cfxmark:payloads -->\n"
    other = "alpha\n\n<!-- cfxmark:payloads -->\n<!-- op-def456\n<time/> -->\n<!-- /cfxmark:payloads -->\n"
    assert _append_comment_position_view(one) != _append_comment_position_view(other)
