"""What a three-way merge must get right before it is allowed near a page.

Measured across 55 live pages: every managed push against a page someone else had
touched came back `remote_stale`, 55 out of 55, with no way forward. Most of those
are not conflicts -- a typo fixed three sections away from the paragraph being
edited -- so a merge turns a dead end into one step.

The tests below are ordered by what they cost if wrong. Losing a remote edit is
the worst outcome and comes first; producing a merge nobody asked for is second;
refusing a merge that was safe is third and merely annoying.
"""

from __future__ import annotations

import pytest

from atlassian_skills.confluence.merge import merge3

BASE = "alpha\nbravo\ncharlie\ndelta\necho\n"


def test_edits_far_apart_merge_into_one_document() -> None:
    """The case that makes this worth building. Two people touched the same page
    and neither touched the other's paragraph."""

    local = BASE.replace("alpha", "alpha edited")
    remote = BASE.replace("echo", "echo edited")
    result = merge3(BASE, local, remote)
    assert result.clean
    assert result.require_clean() == "alpha edited\nbravo\ncharlie\ndelta\necho edited\n"


def test_both_sides_changing_the_same_line_is_a_conflict() -> None:
    """Neither version is more correct than the other, so choosing is not the
    merge's decision to make."""

    result = merge3(BASE, BASE.replace("charlie", "mine"), BASE.replace("charlie", "theirs"))
    assert not result.clean
    (conflict,) = result.conflicts
    assert conflict.local == ("mine\n",)
    assert conflict.remote == ("theirs\n",)


def test_a_conflict_anywhere_abandons_the_whole_merge() -> None:
    """A partial merge would have to write conflict markers into the document, and
    a marker published to a page reads as content. Refusing entirely is the only
    outcome that cannot be mistaken for text somebody wrote."""

    local = BASE.replace("alpha", "mine").replace("charlie", "local change")
    remote = BASE.replace("charlie", "remote change")
    result = merge3(BASE, local, remote)
    assert not result.clean
    assert result.merged is None
    with pytest.raises(ValueError):
        result.require_clean()


def test_adjacent_edits_conflict_rather_than_guessing_an_order() -> None:
    """A replacement ending where an insertion begins has no unambiguous order.
    Picking one silently is how a merge invents a sentence nobody wrote."""

    local = "alpha\nbravo\nCHARLIE\ndelta\necho\n"
    remote = "alpha\nbravo\ncharlie\ninserted\ndelta\necho\n"
    assert not merge3(BASE, local, remote).clean


# --------------------------------------------------------------------------
# Cases where there is nothing to merge
# --------------------------------------------------------------------------


def test_identical_edits_are_not_a_conflict() -> None:
    """Two people making the same correction is agreement, not collision."""

    same = BASE.replace("charlie", "fixed")
    assert merge3(BASE, same, same).require_clean() == same


def test_an_untouched_side_yields_the_other() -> None:
    local = BASE.replace("alpha", "edited")
    assert merge3(BASE, local, BASE).require_clean() == local
    assert merge3(BASE, BASE, local).require_clean() == local


def test_no_edit_at_all_returns_the_base() -> None:
    assert merge3(BASE, BASE, BASE).require_clean() == BASE


# --------------------------------------------------------------------------
# Shapes that appear in real documents
# --------------------------------------------------------------------------


def test_an_insertion_on_each_side_keeps_both() -> None:
    local = BASE.replace("bravo\n", "bravo\nlocal line\n")
    remote = BASE.replace("delta\n", "delta\nremote line\n")
    merged = merge3(BASE, local, remote).require_clean()
    assert "local line" in merged
    assert "remote line" in merged
    assert merged.index("local line") < merged.index("remote line")


def test_a_deletion_on_one_side_survives_an_edit_on_the_other() -> None:
    local = BASE.replace("bravo\n", "")
    remote = BASE.replace("echo", "echo edited")
    merged = merge3(BASE, local, remote).require_clean()
    assert "bravo" not in merged
    assert "echo edited" in merged


def test_deleting_the_lines_the_other_side_edited_is_a_conflict() -> None:
    """One side says this paragraph should go, the other says it should read
    differently. That is a disagreement about the document, not about text."""

    local = BASE.replace("charlie\n", "")
    remote = BASE.replace("charlie", "charlie edited")
    assert not merge3(BASE, local, remote).clean


def test_a_document_with_no_trailing_newline_still_merges() -> None:
    """Managed Markdown files are not guaranteed to end in a newline, and a merge
    that only worked on well-formed input would fail on the first real file that
    was not."""

    base = "alpha\nbravo"
    result = merge3(base, "alpha edited\nbravo", base)
    assert result.require_clean() == "alpha edited\nbravo"


def test_an_empty_base_takes_whichever_side_wrote_something() -> None:
    assert merge3("", "content\n", "").require_clean() == "content\n"


def test_both_sides_writing_into_an_empty_base_conflict() -> None:
    assert not merge3("", "mine\n", "theirs\n").clean


def test_a_conflict_says_where_in_the_base_it_happened() -> None:
    """So a caller can point at the document rather than at an opaque hunk
    number. A conflict report nobody can locate is a refusal with extra steps."""

    (conflict,) = merge3(BASE, BASE.replace("delta", "mine"), BASE.replace("delta", "theirs")).conflicts
    assert BASE.splitlines(keepends=True)[conflict.base_start] == "delta\n"
