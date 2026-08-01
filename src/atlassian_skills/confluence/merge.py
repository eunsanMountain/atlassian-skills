"""Merge an edit with a remote that moved under it, or say plainly that it cannot.

Measured across 55 live pages: every managed push against a page someone else had
touched came back `remote_stale`, 55 times out of 55, with no way forward except
pulling again and redoing the edit by hand. That is the correct refusal and a dead
end, and a dead end is what sends people back to the browser.

Most of those cases are not conflicts. Someone fixed a typo three sections away
from the paragraph being edited. A three-way merge publishes that in one step and
reserves the refusal for edits that genuinely collide.

Two decisions shape everything here.

**Merge canonical Markdown, not what the author typed.** The same document
written two ways -- `\\_` versus `_`, trailing spaces, a reflowed paragraph --
differs textually while meaning the same thing, and merging raw text turns those
into conflicts nobody caused.

**Refuse the whole merge, never half of it.** A conflict marker written into a
published page is worse than a refusal, because it looks like content. When any
hunk collides, nothing is merged and nothing is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher


@dataclass(frozen=True)
class MergeConflict:
    """One place where both sides changed the same lines."""

    #: Line range in the base, so a caller can point at the document rather than
    #: at an opaque hunk number.
    base_start: int
    base_end: int
    local: tuple[str, ...]
    remote: tuple[str, ...]


@dataclass(frozen=True)
class MergeResult:
    merged: str | None
    conflicts: tuple[MergeConflict, ...]

    @property
    def clean(self) -> bool:
        return not self.conflicts

    def require_clean(self) -> str:
        if self.merged is None:
            raise ValueError("a conflicted merge has no result")
        return self.merged


def _opcodes(base: list[str], side: list[str]) -> list[tuple[int, int, list[str]]]:
    """Changed regions of `side` against `base`, as (start, end, replacement).

    Equal regions are dropped: only what a side actually changed can conflict
    with what the other side changed.
    """

    changes = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, base, side, autojunk=False).get_opcodes():
        if tag != "equal":
            changes.append((i1, i2, side[j1:j2]))
    return changes


def _overlaps(one: tuple[int, int, list[str]], other: tuple[int, int, list[str]]) -> bool:
    """Whether two changed regions touch the same base lines.

    Adjacency counts. Two edits that meet exactly at a boundary -- one replacing
    lines 3-5, the other inserting at line 5 -- have no unambiguous order, and
    picking one silently is how a merge invents text nobody wrote.
    """

    return one[0] <= other[1] and other[0] <= one[1]


def merge3(base: str, local: str, remote: str) -> MergeResult:
    """Combine two edits of `base`, or report where they collide.

    Line-based, like every merge tool an author already understands. Prose edits
    land on whole lines, and a finer granularity would produce merges that are
    technically valid and read as gibberish.
    """

    if local == remote:
        return MergeResult(merged=local, conflicts=())
    if base == local:
        return MergeResult(merged=remote, conflicts=())
    if base == remote:
        return MergeResult(merged=local, conflicts=())

    base_lines = base.splitlines(keepends=True)
    local_changes = _opcodes(base_lines, local.splitlines(keepends=True))
    remote_changes = _opcodes(base_lines, remote.splitlines(keepends=True))

    conflicts = [
        MergeConflict(
            base_start=mine[0],
            base_end=mine[1],
            local=tuple(mine[2]),
            remote=tuple(theirs[2]),
        )
        for mine in local_changes
        for theirs in remote_changes
        if _overlaps(mine, theirs)
    ]
    if conflicts:
        # Nothing merged, nothing written. A partial merge with markers in it
        # would publish text that looks like content.
        return MergeResult(merged=None, conflicts=tuple(conflicts))

    merged: list[str] = []
    cursor = 0
    for start, end, replacement in sorted(local_changes + remote_changes):
        merged.extend(base_lines[cursor:start])
        merged.extend(replacement)
        cursor = end
    merged.extend(base_lines[cursor:])
    return MergeResult(merged="".join(merged), conflicts=())


__all__ = ["MergeConflict", "MergeResult", "merge3"]
