"""Every command the product tells a caller to run must be one `--help` admits exists.

A refusal carries `next_actions`, each with an `argv` the caller is meant to run, and
SKILL.md instructs an agent to execute them. Four of those pointed at `hidden=True`
commands: `confluence page md prepare-merge` and `md finalize-merge` (the whole
three-way-merge recovery from a page that moved), and the Jira `finalize-merge`
spelling. Typer hides a command from `--help` without disabling it, so each still ran --
but an agent that discovers this CLI by reading its help, which is how an agent
discovers a CLI, reached a refusal whose only stated way out did not appear to exist.

Reading the argv out of the source rather than out of a curated list is the point. A
hand-listed set of call sites cannot fail for the case nobody thought to list, and the
case nobody thought to list is what happened.

The scan reads three shapes, not one. The first version read only the finished
`{"argv": [...]}` dict, which left `core/errors.py::consent_retry_action` unchecked --
it returns `{"argv": [*argv, option, fingerprint]}`, so its command path is a starred
name and its real origin is a list literal three modules away in the CLI layer. Those
origins are scanned now.

A group's help string is prose rather than an action and is checked separately at the
bottom of this file; it went stale in exactly the same way and nothing here saw it.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
import typer

SRC = Path(__file__).resolve().parents[2] / "src" / "atlassian_skills"

#: Well below the number found today. A scanner that silently stops matching -- a
#: refactor moves `argv` behind a helper, `ast.Dict` stops being the shape -- would
#: otherwise pass by finding nothing, which is the failure mode this whole file exists
#: to avoid one level up.
_MINIMUM_SITES = 30


def _leading_literal_path(argv: ast.List | ast.Tuple) -> list[str]:
    """The command path an argv names: its leading run of plain string constants.

    Stops at the first non-constant, because that is where arguments begin -- `page_id`,
    `str(path)`. Stops at the first `-` for the same reason: an option is not a
    subcommand. Typer resolves a path exactly this far and no further.
    """

    parts: list[str] = []
    for element in argv.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            break
        if element.value.startswith("-"):
            break
        parts.append(element.value)
    return parts


def _argv_literals(tree: ast.AST) -> list[tuple[int, ast.List | ast.Tuple]]:
    """Every literal in this module that becomes the command part of an action.

    Three shapes, because an action is not always written where it is returned. The
    first is the finished form; the other two are origins, and missing them was a real
    blind spot -- `consent_retry_action` returns `{"argv": [*argv, option, fingerprint]}`,
    whose leading elements are a starred name and therefore invisible to a scan that only
    reads the finished dict.

        {"argv": [...]}                 the action as returned
        next_action = [...]             the CLI builds one, then passes it down
        f(..., next_action_argv=(...))  and hands it over by keyword
    """

    found: list[tuple[int, ast.List | ast.Tuple]] = []

    def literal(node: ast.AST) -> ast.List | ast.Tuple | None:
        return node if isinstance(node, (ast.List, ast.Tuple)) else None

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and len(node.keys) == len(node.values):
            for key, value in zip(node.keys, node.values, strict=False):
                if isinstance(key, ast.Constant) and key.value == "argv" and (found_value := literal(value)):
                    found.append((node.lineno, found_value))
        elif isinstance(node, ast.Assign):
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if any(name == "next_action" or name == "argv" or name.endswith("_argv") for name in names):
                if value_node := literal(node.value):
                    found.append((node.lineno, value_node))
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "next_action_argv" and (value_node := literal(keyword.value)):
                    found.append((node.lineno, value_node))
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if name == "consent_retry_action" and node.args and (value_node := literal(node.args[0])):
                found.append((node.lineno, value_node))
    return found


def _argv_sites() -> list[tuple[Path, int, list[str]]]:
    """Every command path the shipped source builds for an action, with where it is."""

    sites: list[tuple[Path, int, list[str]]] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, value in _argv_literals(tree):
            parts = _leading_literal_path(value)
            # Some are written to be run as a shell line and lead with the binary name;
            # the Click tree starts one element in.
            if parts and parts[0] == "atls":
                parts = parts[1:]
            if parts:
                sites.append((path.relative_to(SRC.parents[1]), lineno, parts))
    return sites


def _resolve(root: Any, parts: list[str]) -> tuple[Any, list[str]] | None:
    """Walk the Click tree the way argv does, returning the leaf it lands on."""

    node = root
    for index, part in enumerate(parts):
        children = getattr(node, "commands", None)
        if not children or part not in children:
            return None
        node = children[part]
        if not getattr(node, "commands", None):
            return node, parts[: index + 1]
    return node, parts


@pytest.fixture(scope="module")
def cli_root() -> Any:
    from atlassian_skills.cli.main import app

    return typer.main.get_command(app)


def test_the_scanner_finds_the_argv_sites_it_is_supposed_to_check() -> None:
    """A gate that inspects nothing passes everything."""

    sites = _argv_sites()
    assert len(sites) >= _MINIMUM_SITES, (
        f"found only {len(sites)} argv sites; the extractor has probably stopped matching the shape the source uses"
    )


def test_every_next_action_argv_names_a_command_that_exists(cli_root: Any) -> None:
    """A typo in an argv is a dead end no test would otherwise reach."""

    unresolved = [
        f"{path}:{line} -> {' '.join(parts)}"
        for path, line, parts in _argv_sites()
        if _resolve(cli_root, parts) is None
    ]
    assert not unresolved, "next_actions name commands that do not exist:\n  " + "\n  ".join(unresolved)


def test_every_next_action_argv_names_a_command_help_admits_exists(cli_root: Any) -> None:
    """The one that was actually broken: real commands, hidden from the help."""

    concealed = []
    for path, line, parts in _argv_sites():
        resolved = _resolve(cli_root, parts)
        if resolved is None:
            continue
        command, matched = resolved
        if getattr(command, "hidden", False):
            concealed.append(f"{path}:{line} -> {' '.join(matched)}")
    assert not concealed, (
        "next_actions tell a caller to run commands hidden from --help; either unhide them or "
        "point the action at the visible spelling:\n  " + "\n  ".join(concealed)
    )


def test_the_hidden_check_can_actually_fail(cli_root: Any) -> None:
    """Proof the assertion above is load-bearing and not merely true.

    Asked against a command known to be hidden -- a shipped 0.3.x flat spelling kept
    working and kept out of the help -- so that "no concealed argv" means the check
    looked, rather than that `hidden` is never set anywhere.
    """

    resolved = _resolve(cli_root, ["confluence", "page", "pull-md"])
    assert resolved is not None, "the flat compatibility spelling is gone; pick another hidden command"
    assert getattr(resolved[0], "hidden", False) is True


# --------------------------------------------------------------------------
# The same defect one surface over: prose, not argv
# --------------------------------------------------------------------------


def _all_command_words(root: Any) -> set[str]:
    """Every name that is a command somewhere in this CLI."""

    words: set[str] = set()

    def walk(node: Any) -> None:
        for name, child in getattr(node, "commands", {}).items():
            words.add(name)
            walk(child)

    walk(root)
    return words


def _groups(root: Any) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []

    def walk(node: Any, path: tuple[str, ...]) -> None:
        for name, child in getattr(node, "commands", {}).items():
            if getattr(child, "commands", None):
                found.append((" ".join((*path, name)), child))
                walk(child, (*path, name))

    walk(root, ())
    return found


#: Words a group header uses as a workflow step rather than as a command name, even
#: though something elsewhere in the CLI is called that. Exactly one so far: `edit` is
#: what you do to the file between `pull` and `push`, and there is deliberately no
#: command for it -- `jira comment edit` is a different feature that happens to share
#: the word. Keep this list short; anything added to it stops being checked.
_PROSE_STEPS = frozenset({"edit"})


def test_no_group_help_lists_a_command_that_is_not_in_that_group(cli_root: Any) -> None:
    """The renamed commands were renamed; the sentence above them was not.

    `jira issue description md --help` printed "Managed Markdown workflow: pull,
    validate, diff, push" over a command list holding `compare` and no `diff`, and
    running the word it named answered `No such command 'diff'`. The argv gate above
    could not see it: a group header is prose, not an action, and it is the first thing
    a reader of `--help` sees.

    Read from the enumeration after the colon, which is the form these headers use to
    list their own commands, and judged by whether the word is a command anywhere in
    this CLI -- so an ordinary English word in the same list is not mistaken for a
    claim about a command.
    """

    commands = _all_command_words(cli_root)
    offenders = []
    for path, group in _groups(cli_root):
        _head, _, tail = (group.help or "").partition(":")
        if not tail or "," not in tail:
            continue
        for word in (token.strip(" .`") for token in tail.split(",")):
            if word in commands and word not in group.commands and word not in _PROSE_STEPS:
                offenders.append(f"{path} --help says {word!r}, which is not one of its commands")
    assert not offenders, "group help names a command the group does not have:\n  " + "\n  ".join(offenders)


def test_the_help_prose_check_can_actually_fail(cli_root: Any) -> None:
    """Same reason as above: proof the scan reaches the strings it claims to read."""

    groups = dict(_groups(cli_root))
    assert "jira issue description md" in groups
    assert "compare" in (groups["jira issue description md"].help or ""), (
        "the header no longer names its own commands, so the check above has nothing to catch"
    )
    assert "diff" in _all_command_words(cli_root), "`diff` is gone entirely; pick another live command word"
