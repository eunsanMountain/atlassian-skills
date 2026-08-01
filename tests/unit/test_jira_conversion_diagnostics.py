"""What atls knew about a Jira conversion and did not say.

cfxmark answers six questions about every wiki-to-Markdown conversion. atls kept
two of them and dropped the rest at its own boundary, so a caller could not learn
what the conversion had lost, which attachments the body referred to, or whether
the Markdown it had just been handed was safe to publish back.

`attachments` is the one that actively misleads rather than merely withholding.
The Markdown reads `![](design.png)` whether or not that file exists anywhere the
caller can reach. An agent summarising the issue describes a picture it has never
seen; an agent writing the body back publishes a reference to nothing.

The second half of this file is `--body-format`. Every write command compared it
against the single literal `"md"`, so every other value -- including a plausible
typo -- fell through to the branch that publishes the text unconverted.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from atlassian_skills.cli import jira as jira_cli
from atlassian_skills.cli.main import app
from atlassian_skills.core.config import Config
from atlassian_skills.core.errors import ExitCode
from atlassian_skills.core.format.markdown import jira_wiki_to_md_result

JIRA_URL = "https://jira.example.com"
runner = CliRunner()

WITH_IMAGE = "h2. Title\n\n!design.png!\n\nbody text\n"


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLS_DEFAULT_JIRA_URL", JIRA_URL)
    monkeypatch.setenv("ATLS_DEFAULT_JIRA_TOKEN", "test-token")
    monkeypatch.setattr("atlassian_skills.cli.jira.load_config", lambda: Config())


def _issue(description: str) -> dict:
    return {
        "key": "DEMO600-1",
        "id": "10001",
        "fields": {
            "summary": "A sandbox issue",
            "description": description,
            "status": {"name": "Open"},
            "issuetype": {"name": "Task"},
        },
    }


# --------------------------------------------------------------------------
# What the conversion knew
# --------------------------------------------------------------------------


def test_the_result_carries_every_answer_cfxmark_gave() -> None:
    """Not a subset of them. The fields existed and were computed on every read;
    the only thing missing was somewhere to put them."""

    result = jira_wiki_to_md_result(WITH_IMAGE)
    assert result.attachments == ("design.png",)
    assert result.push_safe is True
    assert result.losses == ()
    assert "design.png" in result.markdown


def test_losses_are_kept_apart_from_warnings() -> None:
    """A warning is something to know and a loss is something gone. Concatenated,
    a caller has to tell them apart by reading the wording, which is not a
    contract anybody can rely on."""

    result = jira_wiki_to_md_result(WITH_IMAGE)
    assert result.all_warnings == result.warnings + result.losses
    # And the flattened view stays available, because the display path wants it.
    assert isinstance(result.all_warnings, tuple)


def test_a_conversion_that_failed_is_not_safe_to_publish_back() -> None:
    """The body is still returned -- a caller who asked to read an issue should
    get the issue -- but writing it back would publish the unconverted original
    as though it had been converted."""

    from atlassian_skills.core.format.markdown import jira_wiki_to_md_with_options_result

    class _Boom:
        def __getattr__(self, name: str) -> object:
            raise RuntimeError("conversion exploded")

    import atlassian_skills.core.format.markdown as module

    original = module.cfxmark.from_jira_wiki
    module.cfxmark.from_jira_wiki = lambda *_a, **_k: _Boom().missing  # type: ignore[assignment]
    try:
        result = jira_wiki_to_md_with_options_result("h2. Title")
    finally:
        module.cfxmark.from_jira_wiki = original  # type: ignore[assignment]

    assert result.markdown == "h2. Title"
    assert result.push_safe is False
    assert result.warnings and "conversion failed" in result.warnings[0]


@respx.mock
def test_issue_get_md_json_reports_the_attachments_the_body_names() -> None:
    """The failure this exists for. Without the list, an agent cannot tell a
    picture it has from a picture it only has a reference to."""

    respx.get(f"{JIRA_URL}/rest/api/2/issue/DEMO600-1").mock(return_value=httpx.Response(200, json=_issue(WITH_IMAGE)))
    result = runner.invoke(app, ["--format", "json", "jira", "issue", "get", "DEMO600-1", "--body-repr", "md"])
    assert result.exit_code == 0, result.output
    conversion = json.loads(result.output)["conversion"]
    assert conversion["attachments"] == ["design.png"]
    assert conversion["push_safe"] is True
    assert conversion["losses"] == []
    # The old key keeps its old shape, so anything already reading it is unaffected.
    assert isinstance(conversion["warnings"], list)


@respx.mock
def test_a_body_with_no_conversion_still_answers_the_question() -> None:
    """Absent reads as "this version does not report it"; an empty list reads as
    "we looked and there were none"."""

    respx.get(f"{JIRA_URL}/rest/api/2/issue/DEMO600-1").mock(
        return_value=httpx.Response(200, json=_issue("plain text body\n"))
    )
    result = runner.invoke(app, ["--format", "json", "jira", "issue", "get", "DEMO600-1", "--body-repr", "md"])
    assert result.exit_code == 0, result.output
    conversion = json.loads(result.output)["conversion"]
    assert conversion["attachments"] == []
    assert conversion["push_safe"] is True


# --------------------------------------------------------------------------
# A value the command does not understand
# --------------------------------------------------------------------------


WRITE_COMMANDS = {
    "issue create": ["jira", "issue", "create", "--project", "DEMO600", "--type", "Task", "--summary", "s"],
    "issue update": ["jira", "issue", "update", "DEMO600-1"],
    "comment add": ["jira", "comment", "add", "DEMO600-1"],
    "comment edit": ["jira", "comment", "edit", "DEMO600-1", "10"],
}


@pytest.mark.parametrize("name", sorted(WRITE_COMMANDS))
def test_an_unknown_body_format_is_refused_on_every_write(name: str, tmp_path: Path) -> None:
    """The measured defect. `md` was the only value with a branch, so `markdown`
    -- and every other spelling -- published the text to Jira unconverted, with
    nothing anywhere saying so.

    Checked on all four write commands rather than one, because the branch was
    copied into each of them separately and that is exactly how one gets missed.
    """

    body = tmp_path / "body.md"
    body.write_text("# heading\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["--format", "json", *WRITE_COMMANDS[name], "--body-format", "markdown", "--body-file", str(body)],
    )
    assert result.exit_code == ExitCode.VALIDATION, result.output
    error = json.loads(result.output)["error"]
    assert error["context"]["reason"] == "unknown_body_option"
    assert error["context"]["allowed"] == ["md", "wiki"]
    # Named, so the caller does not guess a second time.
    assert "markdown" in error["message"]


#: The other flag. `--body-format` names a description or comment body;
#: `--comment-format` names a comment attached to a different action, and it
#: lives on different commands. Covering only the first matrix is how the
#: worklog path kept an unvalidated flag while every neighbour gained one.
COMMENT_COMMANDS = {
    "worklog add": ["jira", "worklog", "add", "DEMO600-1", "--time-spent-seconds", "60"],
    "issue transition": ["jira", "issue", "transition", "DEMO600-1", "--transition-id", "11"],
}


@pytest.mark.parametrize("name", sorted(COMMENT_COMMANDS))
def test_an_unknown_comment_format_is_refused_on_every_command_that_takes_one(name: str) -> None:
    """`worklog add` declared the flag and never checked it, so an unknown value
    was not an error -- it simply was not "md", and the comment went out as raw
    Jira wiki. Measured before the fix: the request reached the server.

    The `--body-format` matrix above did not cover it because this is a
    different flag on different commands, which is the seam a per-command matrix
    exists to close.
    """

    result = runner.invoke(
        app,
        ["--format", "json", *COMMENT_COMMANDS[name], "--comment", "**bold**", "--comment-format", "markdown"],
    )
    assert result.exit_code == ExitCode.VALIDATION, result.output
    error = json.loads(result.output)["error"]
    assert error["context"]["reason"] == "unknown_body_option"
    assert error["context"]["option"] == "--comment-format"
    assert error["context"]["allowed"] == ["md", "wiki"]


def test_every_declared_format_option_is_checked_against_its_enum() -> None:
    """The matrices above list commands by hand, so a new one can be added with
    an unchecked flag and nothing fails. This reads the source instead: every
    command that declares a format option must also validate it.

    That is what would have caught the worklog gap the day it appeared.
    """

    source = Path(jira_cli.__file__).read_text(encoding="utf-8")
    unchecked = []
    # One chunk per decorated command, so a flag declared in one command cannot
    # be counted as validated by its neighbour.
    for command in source.split("\n@"):
        if "def " not in command:
            continue
        name = command.split("def ", 1)[1].split("(")[0]
        validated = {call.split(")")[0] for call in command.split("_checked_choice(")[1:]}
        for option in ("--body-format", "--comment-format", "--body-repr"):
            if f'"{option}"' not in command:
                continue
            if not any(f'"{option}"' in call for call in validated):
                unchecked.append(f"{name}:{option}")
    assert not unchecked, f"format options declared but never validated: {unchecked}"


@pytest.mark.parametrize("value", ["md", "wiki"])
def test_the_accepted_values_still_pass(value: str, tmp_path: Path) -> None:
    """A refusal that also refuses the valid values is worse than none. This gets
    as far as the network, which is where an unmocked test stops."""

    body = tmp_path / "body.md"
    body.write_text("# heading\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["--format", "json", "jira", "issue", "update", "DEMO600-1", "--body-format", value, "--body-file", str(body)],
    )
    assert result.exit_code != ExitCode.VALIDATION or "unknown_body_option" not in result.output


def test_an_unknown_body_repr_is_refused_on_read(tmp_path: Path) -> None:
    """The read side of the same defect. `--body-repr markdown` silently returned
    the stored wiki markup, which a caller would then read as Markdown."""

    result = runner.invoke(app, ["--format", "json", "jira", "issue", "get", "DEMO600-1", "--body-repr", "markdown"])
    assert result.exit_code == ExitCode.VALIDATION, result.output
    error = json.loads(result.output)["error"]
    assert error["context"]["option"] == "--body-repr"
    assert error["context"]["allowed"] == ["md", "raw", "wiki"]


# --------------------------------------------------------------------------
# One write contract, not five
#
# These commands were written separately and drifted apart. Each case below is
# a place where two of them did different things with the same input, and the
# difference tracked which file the code was in rather than anything about the
# operation.
# --------------------------------------------------------------------------


def test_fields_json_cannot_quietly_replace_the_converted_body(tmp_path: Path) -> None:
    """The worst of them. The body was read, converted, and checked for
    conversion losses -- and then replaced:

        --body-format md --body-file b.md --fields-json '{"description": "..."}'
        PUT {"fields": {"description": "..."}}

    The Markdown never reached the server. Every safety check on the write path
    had run against a body that was not published, and nothing said so.
    """

    body = tmp_path / "b.md"
    body.write_text("# Heading\n\nsome text\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "--format",
            "json",
            "jira",
            "issue",
            "update",
            "DEMO600-1",
            "--body-format",
            "md",
            "--body-file",
            str(body),
            "--fields-json",
            '{"description": "I replaced it"}',
            "--dry-run",
        ],
    )
    assert result.exit_code == ExitCode.VALIDATION, result.output
    error = json.loads(result.output)["error"]
    assert error["context"]["reason"] == "fields_json_conflict"
    assert error["context"]["fields"] == ["description"]


def test_fields_json_still_carries_fields_the_command_did_not_set(tmp_path: Path) -> None:
    """The refusal must not become a blanket one. Setting an assignee alongside
    a body is the ordinary case, and the sandbox requires it."""

    body = tmp_path / "b.md"
    body.write_text("# Heading\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "jira",
            "issue",
            "update",
            "DEMO600-1",
            "--body-format",
            "md",
            "--body-file",
            str(body),
            "--fields-json",
            '{"assignee": {"name": "someone"}}',
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "assignee" in result.output
    assert "Heading" in result.output


def test_fields_json_that_is_not_an_object_is_refused() -> None:
    """`'["a"]'` parsed fine and then `dict.update` raised something the caller
    could not act on."""

    result = runner.invoke(
        app, ["--format", "json", "jira", "issue", "update", "DEMO600-1", "--fields-json", '["a"]', "--dry-run"]
    )
    assert result.exit_code == ExitCode.VALIDATION, result.output
    assert json.loads(result.output)["error"]["context"]["reason"] == "fields_json_not_an_object"


def test_a_transition_comment_converts_markdown_like_every_other_comment() -> None:
    """`worklog add` has taken `--comment-format md` since it was written and
    this took the comment raw, so the same Markdown produced two different
    things on the issue depending on which command published it."""

    result = runner.invoke(
        app,
        [
            "jira",
            "issue",
            "transition",
            "DEMO600-1",
            "--transition-id",
            "31",
            "--comment",
            "some **bold** text",
            "--comment-format",
            "md",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    # Jira wiki spells bold with single asterisks.
    assert "*bold*" in result.output
    assert "**bold**" not in result.output


def test_a_transition_comment_defaults_to_wiki_like_the_others() -> None:
    result = runner.invoke(
        app,
        [
            "jira",
            "issue",
            "transition",
            "DEMO600-1",
            "--transition-id",
            "31",
            "--comment",
            "some *bold* text",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "some *bold* text" in result.output


def test_an_unknown_transition_comment_format_is_refused() -> None:
    result = runner.invoke(
        app,
        [
            "--format",
            "json",
            "jira",
            "issue",
            "transition",
            "DEMO600-1",
            "--transition-id",
            "31",
            "--comment",
            "x",
            "--comment-format",
            "markdown",
            "--dry-run",
        ],
    )
    assert result.exit_code == ExitCode.VALIDATION, result.output
    assert json.loads(result.output)["error"]["context"]["option"] == "--comment-format"


@respx.mock
def test_an_update_returns_the_timestamp_the_next_write_needs() -> None:
    """`--if-updated` is how a caller avoids overwriting somebody. Without the
    new value in the output they have to re-read to get it, and a caller who has
    to work for the flag stops passing it.

    Measured on the sandbox: the field carries milliseconds and three writes in
    a row produced three distinct values, so comparing it exactly is sound.
    """

    respx.put(f"{JIRA_URL}/rest/api/2/issue/DEMO600-1").mock(return_value=httpx.Response(204))
    respx.get(url__regex=rf"{JIRA_URL}/rest/api/2/issue/DEMO600-1").mock(
        return_value=httpx.Response(
            200,
            json={"key": "DEMO600-1", "fields": {"updated": "2026-07-29T17:58:22.302+0900", "description": "x"}},
        )
    )
    result = runner.invoke(
        app,
        ["--format", "json", "jira", "issue", "update", "DEMO600-1", "--fields-json", '{"summary": "new"}'],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["updated"] == "2026-07-29T17:58:22.302+0900"


@respx.mock
def test_an_update_says_when_the_server_kept_a_different_body(tmp_path: Path) -> None:
    """There was no readback at all. Confluence rewrites what it stores; the
    sandbox probe found Jira storing bodies byte for byte, which is a reason to
    expect a match rather than a reason not to look."""

    body = tmp_path / "b.txt"
    body.write_text("what was sent\n", encoding="utf-8")
    respx.put(f"{JIRA_URL}/rest/api/2/issue/DEMO600-1").mock(return_value=httpx.Response(204))
    respx.get(url__regex=rf"{JIRA_URL}/rest/api/2/issue/DEMO600-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "key": "DEMO600-1",
                "fields": {"updated": "2026-07-29T17:58:22.302+0900", "description": "what the server kept"},
            },
        )
    )
    result = runner.invoke(
        app,
        ["--format", "json", "jira", "issue", "update", "DEMO600-1", "--body-file", str(body)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    # stdout, not output: the warning goes to stderr and `output` merges them.
    payload = json.loads(result.stdout)
    assert payload["description_matches_sent"] is False
    assert payload["stored_description"] == "what the server kept"
    assert "the server stored a different body than was sent" in result.stderr
