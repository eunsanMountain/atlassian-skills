"""Two spellings, one implementation.

The command names existed but the shape did not. `get` and `update` are single
actions; `pull-md`, `push-xhtml`, `prepare-merge` and the rest are two managed
workflows -- and nothing in the naming said which was which, so a reader had to
know the answer before reading. Grouping them says it.

The risk in adding a second spelling is that it becomes a second implementation
and the two drift. These tests pin that it cannot: the group registers the same
function objects, so there is one body of code, one set of options, one
behaviour. A copy would pass a "does it run" test and fail these.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from atlassian_skills.cli.main import app

runner = CliRunner()

#: Old spelling -> new spelling, for the managed commands the group re-registers.
ALIASES = {
    "pull-md": ("md", "pull"),
    "push-md": ("md", "push"),
    "validate-local": ("md", "validate"),
    "pull-batch": ("md", "pull-batch"),
    "prepare-merge": ("md", "prepare-merge"),
    "finalize-merge": ("md", "finalize-merge"),
    "pull-xhtml": ("xhtml", "pull"),
    "push-xhtml": ("xhtml", "push"),
    "validate-xhtml": ("xhtml", "validate"),
    "diff-xhtml": ("xhtml", "compare"),
    "set-authority": ("xhtml", "set-authority"),
}

#: Hidden flat spellings the group deliberately does NOT re-register.
#:
#: `diff-local` answers "what did I change against my base". `md compare` answers "how do
#: the base, my file and the page stand against each other" -- three-way, so an edit
#: somebody else made shows up instead of being discovered by a refused push. Both were
#: briefly in the group, as `md diff` and `md compare`, one letter apart and giving
#: different answers, with the one that cannot see a remote edit named `diff`.
#:
#: So the group offers the better question only. `diff-local` keeps working for anything
#: that already types it, which is the whole reason a hidden spelling exists.
NO_GROUPED_SPELLING = {"diff-local"}


def _command(path: tuple[str, ...]):
    """Walk to a command by the argv a user would type.

    Reachability is asked as "does this node hold subcommands", not as
    `isinstance(node, click.Group)`. Typer vendored Click at 0.26, so the
    isinstance form compares against a class Typer no longer builds from and
    answers False for every group -- a failure that reads as "the command is
    gone" when the command is right there.
    """

    from typer.main import get_command

    node = get_command(app)
    for name in path:
        assert hasattr(node, "commands"), f"{name} is not reachable under {path}"
        node = node.commands[name]
    return node


def _registered(typer_app, name: str):
    """The function as *registered*, not as built.

    Typer generates a fresh click wrapper every time an app is turned into a
    command, so comparing the built callbacks compares Typer's internals and not
    what this module did. What this module did is register a function under a
    second name, and that is what has to be checked.
    """

    for info in typer_app.registered_commands:
        if info.name == name:
            return info.callback
    raise AssertionError(f"{name} is not registered")


@pytest.mark.parametrize(("old", "new"), sorted(ALIASES.items()))
def test_both_spellings_are_the_same_function(old: str, new: tuple[str, str]) -> None:
    """Identity, not equivalence. Two functions that merely behave alike today
    are two functions to keep in step forever."""

    from atlassian_skills.cli.confluence import md_app, page_app, xhtml_app

    group = md_app if new[0] == "md" else xhtml_app
    assert _registered(page_app, old) is _registered(group, new[1])


@pytest.mark.parametrize(("old", "new"), sorted(ALIASES.items()))
def test_both_spellings_take_the_same_options(old: str, new: tuple[str, str]) -> None:
    """Follows from the above, and asserted separately because it is the thing a
    caller actually depends on when moving between the two."""

    legacy = {param.name for param in _command(("confluence", "page", old)).params}
    grouped = {param.name for param in _command(("confluence", "page", *new)).params}
    assert legacy == grouped


def test_every_managed_command_has_a_grouped_spelling() -> None:
    """The point of the grouping is that a reader can see the whole workflow in
    one place. A command left outside it is one the reader will not find there,
    and will conclude does not exist."""

    page = _command(("confluence", "page"))
    grouped = {
        name
        for group in ("md", "xhtml")
        for name in page.commands[group].commands  # type: ignore[union-attr]
    }
    for old, (_group, new_name) in ALIASES.items():
        assert new_name in grouped, f"{old} has no grouped spelling"


def test_the_group_offers_one_comparison_and_it_is_the_three_way_one() -> None:
    """`diff-local` is left out of the group on purpose, and this says so out loud.

    A `md diff` next to `md compare` is two commands one letter apart answering
    different questions, and the one that reads like the obvious choice -- `diff` -- is
    the one that compares against the base and cannot see an edit somebody else made.
    Read as "nothing differs" that is a refused push at best.

    Asserted rather than left as a comment, because "we removed it" and "it fell out in
    a refactor" look identical six months later.
    """

    md = _command(("confluence", "page", "md"))
    assert "compare" in md.commands  # type: ignore[union-attr]
    assert "diff" not in md.commands  # type: ignore[union-attr]

    # And the flat spelling still runs, which is what makes leaving it out safe.
    assert runner.invoke(app, ["confluence", "page", "diff-local", "--help"]).exit_code == 0


def test_recover_assets_is_deliberately_not_in_the_markdown_group() -> None:
    """It repairs a state-free create's images and takes an ordinary body file.
    Under `md` it would read as part of the managed workflow, which it is not --
    and a caller who believed that would look for a manifest that is not there."""

    page = _command(("confluence", "page"))
    assert "recover-assets" in page.commands
    assert "recover-assets" not in page.commands["md"].commands  # type: ignore[union-attr]


def test_the_groups_describe_themselves_in_help() -> None:
    """The whole reason the grouping exists is that a reader could not tell a
    single action from a workflow. Unlabelled groups would move the commands
    without answering that."""

    result = runner.invoke(app, ["confluence", "page", "--help"])
    assert result.exit_code == 0
    assert "md" in result.output
    assert "xhtml" in result.output


@pytest.mark.parametrize("group", ["md", "xhtml"])
def test_a_group_with_no_arguments_shows_its_commands(group: str) -> None:
    """Rather than failing. Someone typing `page md` is asking what is in it."""

    result = runner.invoke(app, ["confluence", "page", group])
    assert "pull" in result.output
    assert "push" in result.output


# --------------------------------------------------------------------------
# One documented spelling
# --------------------------------------------------------------------------


def _hidden_command_names() -> set[str]:
    from typer.main import get_command

    page = get_command(app).commands["confluence"].commands["page"]  # type: ignore[union-attr]
    return {name for name, command in page.commands.items() if command.hidden}  # type: ignore[union-attr]


def test_the_legacy_spellings_are_hidden_but_still_work() -> None:
    """Both halves. Hiding without keeping them would break scripts; keeping
    them visible would leave the confusion the grouping was added to remove."""

    assert _hidden_command_names() == set(ALIASES) | NO_GROUPED_SPELLING

    result = runner.invoke(app, ["confluence", "page", "--help"])
    for legacy in (*ALIASES, *NO_GROUPED_SPELLING):
        assert f" {legacy} " not in result.output

    # Still reachable, and still carrying its own help.
    assert runner.invoke(app, ["confluence", "page", "pull-md", "--help"]).exit_code == 0


def test_the_groups_are_the_documented_surface() -> None:
    result = runner.invoke(app, ["confluence", "page", "--help"])
    assert "md " in result.output
    assert "xhtml " in result.output


def test_no_returned_command_names_a_hidden_spelling() -> None:
    """The rule is to run the argv exactly. An argv naming a command that no
    longer appears in help sends the caller looking for something they cannot
    find, and is the first thing that would rot after hiding the old names.

    Judged by *position*, not by token. `prepare-merge` is both a hidden
    top-level spelling and a legitimate verb inside `md`, so a scan that only
    looked for the word flagged the correct calls -- which is how a check like
    this gets deleted for crying wolf.
    """

    import atlassian_skills

    hidden = _hidden_command_names()
    root = Path(atlassian_skills.__file__).parent
    quoted = re.compile(r'"([a-z][a-z0-9-]*)"')
    offenders = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r'"confluence",\s*"page",\s*', source):
            following = quoted.search(source, match.end())
            if following is None:
                continue
            name = following.group(1)
            if name in hidden:
                line = source.count("\n", 0, following.start()) + 1
                offenders.append(f"{path.relative_to(root)}:{line} -> {name}")
    assert not offenders, f"returned command names a hidden spelling: {offenders}"


@pytest.mark.parametrize(
    "storage",
    ["<p>alpha bravo charlie</p>", '<table><tbody><tr><td colspan="2">a</td></tr></tbody></table>'],
    ids=["markdown", "storage"],
)
def test_every_compatibility_action_uses_a_visible_command(storage: str) -> None:
    """Measured on the payload as well as the source, because the source scan
    proves nothing about a path assembled at runtime."""

    from typer.main import get_command

    from atlassian_skills.confluence.compatibility import compatibility_payload

    page = get_command(app).commands["confluence"].commands["page"]  # type: ignore[union-attr]
    for action in compatibility_payload("123", storage, document_path="page.md")["next_actions"]:
        argv = action["argv"]
        assert argv[:2] == ["confluence", "page"]
        command = page.commands[argv[2]]  # type: ignore[union-attr]
        assert not command.hidden, argv
