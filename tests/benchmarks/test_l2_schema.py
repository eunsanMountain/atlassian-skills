"""L2 token schema benchmarks: the canonical skill has a budget and must keep it.

The ceiling was 2000 while the skill described one product's managed workflow.
It now describes two, and the Jira section is 251 tokens of things an agent gets
wrong on its own -- chiefly that a body can be perfectly safe to read and unsafe
to publish back.

Fitting that under 2000 meant deleting about 200 tokens of Confluence guidance,
and what is there is branches a caller needs: the `patch-text` failure reasons,
the push proof order, the merge and storage workflows. Raising the ceiling to
cover new content is not the same as raising it to hide bloat, and the way to
keep those apart is that the number moves once, deliberately, with the reason
written down.

The budget still exists and still fails the build. An agent loads this before its
first command, so every token is paid on every task.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.benchmarks.scenarios import count_tokens

ASSETS_DIR = Path(__file__).parent.parent.parent / "src" / "atlassian_skills" / "_assets"

#: Raised from 2000 when the skill took on the Jira read and write contracts, and
#: from 2400 for the managed description commands in 0.3.4 -- eleven commands
#: whose whole value is that they refuse things, and a refusal an agent cannot
#: anticipate costs more than the tokens spent saying it will happen.
#:
#: A budget nobody may raise stops being a budget and starts being a reason to
#: write the documentation somewhere the budget does not look. The rule this
#: keeps is narrower and worth more: raise it for a command surface that exists,
#: never to make room for prose about one that already does. Every raise so far
#: has come with new commands in the same commit.
#:
#: 2900 in this release, for `md pull`'s new refusal: a page whose grade forbids the
#: canonical write returns `not_pulled` and writes nothing, and `--accept-migration`
#: is the only way past it. That is new surface, and it is the case the note above
#: was written for -- a refusal an agent cannot anticipate costs more than the
#: tokens spent saying it will happen. An agent that does not know `not_pulled`
#: exists reads `path: null` as a failure and retries.
#:
#: Three sentences in the same commit are corrections rather than new surface --
#: the manifest version, what the sidecar is for, and when `base` is absent had all
#: become false as the code moved. They cost tokens because the true statement is
#: longer than the false one was, which the rule above does not anticipate. Noted
#: rather than smuggled: if that is not an acceptable reason to raise a budget, the
#: place to cut is those three, and the file will be wrong again.
#: 3000 in P8, for §7.1's four reconciliation commands -- `compare`,
#: `prepare-reconcile`, `record-reconciled-against`, `rebaseline`. Four commands the
#: previous skill did not mention at all: it documented `prepare-merge`/`finalize-merge`,
#: which this release demoted to hidden aliases, so an agent following the skill was
#: reaching for commands the help no longer lists and could not do the stale flow.
#:
#: This is the case the note above permits and the previous raise only half was. 2800 ->
#: 2900 in U4 covered `not_pulled` and `--accept-migration`, which is new surface, *and*
#: three corrections, which are not; that was recorded at the time as the weaker half.
#: This one is four commands and nothing else.
#:
#: If it needs raising a third time, the thing to check first is whether the storage and
#: `patch-text` sections have started repeating the managed ones.
#:
#: 3260 in B4, for the table of files this tool writes to the author's disk: the inline
#: manifest, the `.md.atls.json` cache, the XHTML sidecar, `.reconciled.md`, and the
#: in-flight `atls:operation` comment -- with, for each, whether it belongs in Git and
#: whether deleting it is safe.
#:
#: **The check the note above asks for was done first, and the repetition it worried about
#: has not happened.** The storage section is 155 tokens of its own commands and the
#: authority rule; the `patch-text` paragraph is its own `error.context.reason` branches.
#: Neither restates the managed sections, so there was nothing there to reclaim. The one
#: cut available was made in B1: the sidecar's status left the pull bullet, because this
#: table is where it belongs beside every other generated file.
#:
#: Why this is the permitted case rather than the weaker one. `--help` describes commands;
#: nothing in it tells a reader that `.reconciled.md` must not be committed, or that a `.md`
#: carrying an `atls:operation` comment is a publish in flight rather than a draft. Both
#: mistakes are cheap to make and expensive: the first puts a generated intermediate into
#: review, the second discards the only record of what was sent. That is the shape the
#: original note names -- something an agent cannot anticipate, costing more than the tokens
#: spent saying it.
#:
#: 3229 actual. The margin is deliberately small; the next raise should be argued, not
#: absorbed.
#:
#: Raised to 3320 at 0.4.0, and here is the argument. Two of the three costs are forced by
#: correctness rather than chosen:
#:
#:   ~13  the recovery commands are named by their visible spellings now.
#:        `prepare-reconcile` and `record-reconciled-against` are longer than the
#:        `prepare-merge` / `finalize-merge` aliases the file used to print, and those
#:        aliases are hidden -- a reader who typed what the old text said got "no such
#:        command" from `--help`. There is no shorter spelling of the right name.
#:   ~22  one line saying what `compare` means against what `diff` means, in the
#:        conventions list at the top. The alternative is the mistake it prevents: reaching
#:        for `md diff`, which no longer exists, or for `page diff-local`, which compares
#:        against the base and cannot see an edit somebody else made.
#:
#: What was NOT done to fit: trimming the crash-recovery paragraph or the consent rules,
#: which is where the remaining fat nominally is and where the tokens are load-bearing.
#: 3294 actual, 26 of margin -- the same order the previous ceiling carried.
CANONICAL_SKILL_BUDGET = 3320

pytestmark = pytest.mark.benchmark


def test_canonical_skill_within_budget() -> None:
    """L2: the canonical SKILL.md (shared by Claude + Codex) must fit its budget."""

    skill_path = ASSETS_DIR / "skills" / "atls" / "SKILL.md"
    assert skill_path.exists(), f"Canonical skill not found at {skill_path}"
    tokens = count_tokens(skill_path.read_text(encoding="utf-8"))
    print(f"\nL2 canonical SKILL.md: {tokens} tokens")
    assert tokens <= CANONICAL_SKILL_BUDGET, f"Canonical skill: {tokens} > {CANONICAL_SKILL_BUDGET} limit"


def test_the_budget_is_not_slack_waiting_to_be_used() -> None:
    """A ceiling raised to fit a section, then treated as headroom, is a ceiling
    that gets raised again. This fails while there is more than a section's worth
    of room left, so the next increase is a decision rather than a drift."""

    skill_path = ASSETS_DIR / "skills" / "atls" / "SKILL.md"
    tokens = count_tokens(skill_path.read_text(encoding="utf-8"))
    assert tokens >= CANONICAL_SKILL_BUDGET - 400, (
        f"Canonical skill: {tokens} tokens, {CANONICAL_SKILL_BUDGET - tokens} under budget. "
        "Lower CANONICAL_SKILL_BUDGET to what the file needs."
    )
