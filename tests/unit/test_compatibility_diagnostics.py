"""Whether a caller can miss what we measured.

The loss engine was right and unreachable. A person running `pull-md` got the
verdict as one field of a one-line dict while a file landed successfully and the
command exited 0 -- and exiting 0 is correct, because a page Markdown cannot hold
is a fact about the page, not a broken command. An agent got it three levels
down, and one that did not descend published as if the page were plain.

So these tests are about delivery, not measurement. Every one of them asks the
same question from a different seat: *if I were not looking for this, would I
still see it?*

The tests that matter most are the negative ones. A warning on every page teaches
people to skim past all of them, so `markdown_ready` has to be silent -- and a
next action carrying `<file>` is worse than no next action, because the skill's
central rule is to run what is returned and never invent a command.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from atlassian_skills.cli.main import app
from atlassian_skills.confluence.compatibility import compatibility_payload
from tests.unit.test_state_free_body_write import BodyClient

runner = CliRunner()

PAGES = {
    "markdown_ready": "<p>alpha bravo charlie delta</p>",
    "markdown_identity_bound": (
        '<ac:structured-macro ac:name="info" ac:schema-version="1" ac:macro-id="7f3a-0001">'
        "<ac:rich-text-body><p>note body here</p></ac:rich-text-body></ac:structured-macro>"
    ),
    "migration_required": (
        "<p>alpha bravo charlie</p><table><thead><tr><th>h</th></tr></thead>"
        '<tbody><tr><td data-highlight-colour="#ff0000">a</td></tr></tbody></table>'
    ),
    "xhtml_required": (
        '<table><thead><tr><th>h</th></tr></thead><tbody><tr><td colspan="2">a</td></tr></tbody></table>'
    ),
}
RAGGED_TABLE = (
    "<table><thead><tr><th>key</th><th>value</th></tr></thead><tbody>"
    "<tr><td>a</td><td>b</td></tr>"
    "<tr><td>prefix</td><td>left</td><td>delta</td><td>right</td></tr>"
    "</tbody></table><p>After</p>"
)
NEEDS_ATTENTION = tuple(name for name in PAGES if name != "markdown_ready")


def _pull(storage: str, directory: Path, *, extra: tuple[str, ...] = ()) -> tuple[int, str, dict]:
    """Run the command people actually run, and return what each audience sees."""

    client = BodyClient()
    client.storage = storage
    output = directory / "page.md"
    # Patched, not assigned. A bare `module._make_client = ...` stayed in place
    # for the rest of the session, and every later test that reached Confluence
    # through the CLI got this fake instead of a real client -- reported as
    # `'BodyClient' object has no attribute 'reply_to_comment'` in a file that
    # has nothing to do with this one, and only under an order that happens to
    # run this first.
    with patch("atlassian_skills.cli.confluence._make_client", lambda _ctx: client):
        # Global options go before the subcommand -- placing them after is how a
        # first draft of this file measured a `--quiet` that was never applied.
        result = runner.invoke(
            app,
            [*extra, "confluence", "page", "pull-md", "123", "--output", str(output), "--no-assets"],
        )
    payload: dict = {}
    if "--format" in extra:
        for line in result.stdout.splitlines():
            if line.startswith("{"):
                payload = json.loads(line)
                break
    return result.exit_code, result.output, payload


# --------------------------------------------------------------------------
# The person watching the terminal
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", NEEDS_ATTENTION)
def test_a_page_that_needs_attention_says_so_in_words(name: str) -> None:
    """Not in a field of a dict. On stderr, where a diagnostic goes, while stdout
    stays clean for whoever is piping it."""

    with tempfile.TemporaryDirectory() as directory:
        exit_code, output, _ = _pull(PAGES[name], Path(directory))

    assert exit_code == 0, "a classification is not a failed command"
    assert "WARNING" in output or "INFO" in output
    assert name in output


def test_a_page_markdown_holds_says_nothing() -> None:
    """The negative case, and the reason the others are worth reading. A tool
    that comments on every success trains people to skim past the one time it
    matters."""

    with tempfile.TemporaryDirectory() as directory:
        _, output, _ = _pull(PAGES["markdown_ready"], Path(directory))

    assert "WARNING" not in output
    assert "INFO" not in output


def test_the_warning_names_the_loss_its_size_and_where_it_is() -> None:
    """ "Something will be lost" sends the reader back to the JSON. The point of
    the line is that they do not have to go."""

    with tempfile.TemporaryDirectory() as directory:
        _, output, _ = _pull(PAGES["migration_required"], Path(directory))

    assert "table cell background colour" in output
    assert "count=1" in output
    assert "at table" in output


def test_the_warning_names_the_file_that_was_written() -> None:
    """The file lands even when losses exist -- that is deliberate -- so the
    warning has to say which file it is talking about."""

    with tempfile.TemporaryDirectory() as directory:
        _, output, _ = _pull(PAGES["migration_required"], Path(directory))
        assert str(Path(directory) / "page.md") in output


def test_the_warning_explains_the_boundary_of_a_preserved_remote_structure() -> None:
    """A Markdown file that is only partly editable must say so without requiring
    the person watching stderr to discover a JSON field first."""

    with tempfile.TemporaryDirectory() as directory:
        exit_code, output, _ = _pull(RAGGED_TABLE, Path(directory))

    assert exit_code == 0
    assert "remote-only structures are preserved only" in output
    assert "editing them is refused before publishing" in output
    assert "ragged table row/cell topology" in output


def test_the_warning_ends_with_a_command_that_can_be_run() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _, output, _ = _pull(PAGES["migration_required"], Path(directory))

    line = [row for row in output.splitlines() if "next:" in row]
    assert line, "a warning with no way forward is where agents start inventing commands"
    assert "<" not in line[0]


def test_quiet_silences_the_terminal_and_not_the_json() -> None:
    """`--quiet` is a preference about the terminal. The machine's copy of the
    same fact is not a preference, and suppressing it would make the flag a way
    to publish blind."""

    with tempfile.TemporaryDirectory() as directory:
        _, output, payload = _pull(PAGES["migration_required"], Path(directory), extra=("--quiet", "--format", "json"))

    assert "WARNING" not in output
    assert payload["attention_required"] is True
    assert payload["compatibility"]["status"] == "migration_required"


# --------------------------------------------------------------------------
# The agent reading JSON
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", NEEDS_ATTENTION)
def test_the_signal_is_at_the_top_of_the_payload(name: str) -> None:
    """One key, at the top. Everything below was already true three levels down,
    and a caller that does not descend publishes as if the page were plain."""

    with tempfile.TemporaryDirectory() as directory:
        _, _, payload = _pull(PAGES[name], Path(directory), extra=("--format", "json"))

    assert payload["attention_required"] is True
    assert payload["attention_reason"] == name


def test_a_clean_page_says_no_attention_rather_than_omitting_the_key() -> None:
    """Absent reads as "this version does not report it". False reads as "we
    looked"."""

    with tempfile.TemporaryDirectory() as directory:
        _, _, payload = _pull(PAGES["markdown_ready"], Path(directory), extra=("--format", "json"))

    assert payload["attention_required"] is False
    assert payload["attention_reason"] is None


@pytest.mark.parametrize("name", list(PAGES))
def test_no_next_action_anywhere_carries_a_placeholder(name: str) -> None:
    """The rule the whole design rests on is: run the argv exactly, never invent
    a command. An argv containing `<file>` does not inconvenience the caller --
    it puts them in the position the rule forbids."""

    with tempfile.TemporaryDirectory() as directory:
        _, _, payload = _pull(PAGES[name], Path(directory), extra=("--format", "json"))

    actions = payload["compatibility"]["next_actions"]
    assert actions, "a status with no way forward is a dead end"
    for action in actions:
        assert not [item for item in action["argv"] if item.startswith("<")], action


def test_the_storage_workflow_is_told_where_to_write() -> None:
    """An agent asked to invent this path would, and two agents would invent two.
    The command decides it: beside the Markdown file, same stem."""

    with tempfile.TemporaryDirectory() as directory:
        _, _, payload = _pull(PAGES["xhtml_required"], Path(directory), extra=("--format", "json"))

    pulls = [a for a in payload["compatibility"]["next_actions"] if a["argv"][2:4] == ["xhtml", "pull"]]
    assert pulls, "the recommended workflow has to be reachable"
    argv = pulls[0]["argv"]
    assert argv[argv.index("--output") + 1] == str(Path(directory) / "page.xhtml")


# --------------------------------------------------------------------------
# Two decisions, two names
# --------------------------------------------------------------------------


def test_the_pull_asks_which_workflow_and_never_for_publish_consent() -> None:
    """They were one field called `requires_user_approval` in two payloads a
    caller reads minutes apart. Choosing how to manage a page and approving what
    one edit drops are different decisions, and the wrong one gets answered when
    they share a name."""

    payload = compatibility_payload("123", PAGES["migration_required"])
    assert payload["workflow_decision_required"] is True
    assert "requires_user_approval" not in payload
    assert "publish_consent_required" not in payload


def test_the_push_asks_for_publish_consent_against_the_actual_candidate() -> None:
    """And says so at the top. A page can be `migration_required` while this
    particular edit drops nothing -- that is the forecast-versus-bill distinction,
    and it is only useful if the bill is where a caller looks."""

    from atlassian_skills.confluence.migration_preflight import build_managed_preflight
    from atlassian_skills.confluence.pull_md import pull_md  # noqa: F401 - kept for the module's other pulls
    from tests.unit.conftest import pull_managed_accepting_named_losses

    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        client = BodyClient()
        client.storage = PAGES["migration_required"]
        managed = work / "page.md"
        pull_managed_accepting_named_losses(client, "123", managed, no_assets=True)
        managed.write_text(managed.read_text(encoding="utf-8").replace("alpha bravo", "alpha edited"), encoding="utf-8")

        result = build_managed_preflight(client, "123", managed).to_dict()

    assert result["compatibility"]["status"] == "migration_required"
    assert result["compatibility"]["workflow_decision_required"] is True
    # ...and this edit costs nothing, which is the number to ask about.
    assert result["publish_consent_required"] is False


# --------------------------------------------------------------------------
# One name per loss
# --------------------------------------------------------------------------


def test_the_same_loss_has_the_same_name_in_both_payloads() -> None:
    """A dropped cell background was `td@data-highlight-colour` in one payload and
    `table-cell-background` in the other. Nothing was wrong with either; they were
    built separately, which is how vocabularies drift."""

    from atlassian_skills.confluence.compatibility import candidate_loss

    storage = PAGES["migration_required"]
    dropped = storage.replace(' data-highlight-colour="#ff0000"', "")

    forecast = compatibility_payload("123", storage)["findings"][0]
    bill = candidate_loss(storage, dropped)["named_losses"][0]

    assert forecast["canonical_code"] == bill["code"]
    # The measured key is kept too: it is what the comparator recorded, and a
    # reader tracing a finding back needs it.
    assert forecast["code"] == "td@data-highlight-colour"


def test_no_shipped_argv_anywhere_carries_a_placeholder() -> None:
    """The whole source tree, not one payload.

    A previous version of this file had a test with this name that read only the
    compatibility payload -- and a placeholder survived in an error path, which is
    exactly where the caller is already in trouble and least able to improvise.
    A test that claims "anywhere" has to look everywhere.
    """

    import atlassian_skills

    root = Path(atlassian_skills.__file__).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or '"""' in stripped:
                continue
            # An argv element that is a bracketed word is a hole for the caller
            # to fill, which is the thing the run-what-is-returned rule forbids.
            if ('"argv"' in line or "next_action_argv" in line or "argv =" in line) and "<" in line:
                offenders.append(f"{path.relative_to(root)}:{number}")
            if '"<' in line and any(key in line for key in ("argv", "--md-file", "--output", "--xhtml-file")):
                offenders.append(f"{path.relative_to(root)}:{number}")

    assert not offenders, f"placeholder in a shipped command: {offenders}"
