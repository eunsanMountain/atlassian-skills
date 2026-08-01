r"""Two questions about a Jira body, and they have different answers.

This file first asked one and got the direction of the error wrong. It reported
`content_incomplete` for bodies whose every word was present, and told readers
not to summarise from output that was accurate.

The measurement that settled it, over the fifteen shapes the harness puts on a
live server: **every one keeps all of its text.** Headings, panels, expands, a
macro inside a table cell, a description written in Markdown -- not one word is
lost. Seven of the fifteen come back as *different wiki* than they went in, and
that is a separate problem with a separate answer.

The clearest case is the quiet one:

    h3. 개요        reads as    ### 개요        correct
                    writes as   h2. 개요        a different heading

Nothing is wrong with reading that. Publishing it back promotes the heading and
says nothing. So:

    content_complete   did the text reach the Markdown
    write_back_safe    would publishing it leave the issue as it was

Ordered by what it costs to be wrong. A false "do not summarize" is the most
expensive, because it is the warning that gets switched off.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from atlassian_skills.cli.main import app
from atlassian_skills.core.config import Config
from atlassian_skills.core.format.markdown import jira_wiki_to_md_result
from atlassian_skills.jira.read_projection import CONTENT_INCOMPLETE, assess_jira_read

JIRA_URL = "https://jira.example.com"
runner = CliRunner()

#: Everything here reads correctly and publishes back unchanged.
FULLY_SAFE = {
    "plain prose": "just some plain text here",
    "wiki heading": "h2. Direction\n\nbody follows",
    "bullet list": "* one\n* two",
    "numbered list": "# one\n# two",
    "bold": "some *bold* text",
    "code block": "{code:python}\nx = 1\n{code}",
    "link": "[Google|https://google.com]",
    "table": "||h1||h2||\n|a|b|",
    "colour": "{color:red}red text{color}",
}

#: Everything here reads correctly and would change the issue if written back.
READ_SAFE_WRITE_UNSAFE = {
    "h3 heading": "h3. 개요\n\n본문 문단이다.",
    "noformat": "{noformat}\nraw text here\n{noformat}",
    "panel": "{panel:title=Important}body inside{panel}",
    "expand": "{expand:More}hidden body{expand}",
    "code in a table cell": "||h||\n|{code}x{code}|",
    "markdown headings": "## 방향\n1. first\n2. second",
}


def _assess(wiki: str, **kwargs: object):
    conversion = jira_wiki_to_md_result(wiki)
    return assess_jira_read(
        wiki,
        conversion.markdown,
        document=conversion.document,
        losses=conversion.losses,
        attachments=conversion.attachments,
        **kwargs,  # type: ignore[arg-type]
    )


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
# The reading is fine -- say so
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted({**FULLY_SAFE, **READ_SAFE_WRITE_UNSAFE}))
def test_the_text_of_the_body_always_reaches_the_markdown(name: str) -> None:
    """The measurement, pinned. Fifteen shapes on a live server and not one loses
    a word, so a reader summarising from this output is not working from a hole.

    This is the assertion that stops the module drifting back to warning about
    everything: if a future change starts dropping text, it fails here rather
    than being absorbed into a verdict that was already saying no.
    """

    body = {**FULLY_SAFE, **READ_SAFE_WRITE_UNSAFE}[name]
    report = _assess(body)
    assert report.content_complete is True, f"{name} reported missing text: {report.missing_words}"
    assert report.missing_words == ()


@pytest.mark.parametrize("name", sorted(FULLY_SAFE))
def test_a_body_that_survives_both_ways_says_nothing(name: str) -> None:
    report = _assess(FULLY_SAFE[name])
    assert report.write_back_safe is True, f"{name} would change on write-back"
    assert report.attention_required is False
    assert report.reason is None


def test_an_empty_body_is_complete_and_safe() -> None:
    report = assess_jira_read("", "")
    assert report.content_complete is True
    assert report.write_back_safe is True


def test_whitespace_alone_is_not_a_difference() -> None:
    """Byte comparison called every faithful shape a change. A renderer's blank
    lines are not the issue body moving."""

    from atlassian_skills.jira.read_projection import comparable

    assert comparable("a\n\n\nb  \n") == comparable("  \na\nb\n\n")


# --------------------------------------------------------------------------
# The writing is not -- say that instead
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(READ_SAFE_WRITE_UNSAFE))
def test_a_body_that_would_change_on_write_back_is_reported_as_that(name: str) -> None:
    report = _assess(READ_SAFE_WRITE_UNSAFE[name])
    assert report.content_complete is True
    assert report.write_back_safe is False, f"{name} was reported safe to publish"
    assert report.reason == "body_would_change"


def test_the_heading_level_case_is_the_one_that_reads_perfectly() -> None:
    """The case that proves the two questions are different. `### 개요` is exactly
    right; saving it back writes `h2.` and moves the heading up a level."""

    report = _assess(READ_SAFE_WRITE_UNSAFE["h3 heading"])
    assert report.content_complete is True
    assert report.first_difference == ("h3. 개요", "h2. 개요")


def test_the_difference_itself_is_reported_not_just_that_there_is_one() -> None:
    """ "It differs" leaves the caller to go and find where. The pair is the whole
    point: a reader can see at a glance whether they care."""

    payload = _assess(READ_SAFE_WRITE_UNSAFE["noformat"]).to_dict("DEMO600-1")
    assert payload["first_difference"] == {"stored": "{noformat}", "would_write": "{code}"}


def test_a_write_unsafe_body_is_sent_to_the_markup_it_should_edit_instead() -> None:
    report = _assess(READ_SAFE_WRITE_UNSAFE["panel"])
    (action,) = report.to_dict("DEMO600-1")["next_actions"]
    assert action["argv"] == ["jira", "issue", "get", "DEMO600-1", "--fields", "description", "--format=raw"]
    assert "edit that instead" in action["label"]
    assert action["requires_user_approval"] is False


# --------------------------------------------------------------------------
# Missing text, when there is any
# --------------------------------------------------------------------------


def test_text_the_markdown_does_not_hold_is_reported_as_missing() -> None:
    """No shape in the corpus does this, so it is exercised directly rather than
    left as an untested branch that quietly stops working."""

    conversion = jira_wiki_to_md_result("h2. Title\n\nthe body says something")
    report = assess_jira_read(
        "h2. Title\n\nthe body says something",
        "## Title\n",  # the paragraph never made it
        document=conversion.document,
    )
    assert report.content_complete is False
    assert report.reason == "content_incomplete"
    assert "something" in report.missing_words


def test_a_repeated_word_is_counted_not_merely_looked_for() -> None:
    """A body that reuses its vocabulary can lose a sentence with every word in
    it still present somewhere else."""

    conversion = jira_wiki_to_md_result("alpha bravo\n\nalpha bravo")
    report = assess_jira_read("alpha bravo\n\nalpha bravo", "alpha bravo\n", document=conversion.document)
    assert report.content_complete is False


# --------------------------------------------------------------------------
# Asking for part of a body
# --------------------------------------------------------------------------


def test_asking_for_a_section_is_neither_a_loss_nor_a_conversion_problem() -> None:
    report = _assess(FULLY_SAFE["wiki heading"], requested_projection=True)
    assert report.reason == "requested_projection"
    assert report.content_complete is True
    (action,) = report.to_dict("DEMO600-1")["next_actions"]
    assert action["argv"] == ["jira", "issue", "get", "DEMO600-1", "--body-repr=md"]


# --------------------------------------------------------------------------
# Reaching the caller
# --------------------------------------------------------------------------


@respx.mock
def test_issue_get_md_json_separates_the_two_verdicts() -> None:
    respx.get(f"{JIRA_URL}/rest/api/2/issue/DEMO600-1").mock(
        return_value=httpx.Response(200, json=_issue(READ_SAFE_WRITE_UNSAFE["h3 heading"]))
    )
    result = runner.invoke(app, ["--format", "json", "jira", "issue", "get", "DEMO600-1", "--body-repr", "md"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["content_complete"] is True
    assert payload["write_back_safe"] is False
    assert payload["attention_reason"] == "body_would_change"
    assert payload["first_difference"]["would_write"] == "h2. 개요"


@respx.mock
def test_the_stderr_warning_does_not_tell_a_reader_to_distrust_a_good_body() -> None:
    """The correction, at the surface a person actually sees."""

    respx.get(f"{JIRA_URL}/rest/api/2/issue/DEMO600-1").mock(
        return_value=httpx.Response(200, json=_issue(READ_SAFE_WRITE_UNSAFE["h3 heading"]))
    )
    result = runner.invoke(app, ["jira", "issue", "get", "DEMO600-1", "--body-repr", "md"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "body_would_change" in result.stderr
    assert "reading it is fine" in result.stderr
    assert "do not summarize" not in result.stderr
    assert "h2. 개요" in result.stderr


@respx.mock
def test_a_body_that_is_safe_both_ways_gets_no_warning() -> None:
    respx.get(f"{JIRA_URL}/rest/api/2/issue/DEMO600-1").mock(
        return_value=httpx.Response(200, json=_issue(FULLY_SAFE["wiki heading"]))
    )
    result = runner.invoke(app, ["jira", "issue", "get", "DEMO600-1", "--body-repr", "md"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "WARNING" not in result.stderr


@respx.mock
def test_the_json_path_does_not_also_warn_on_stderr() -> None:
    """The verdict is already in the payload. A caller parsing stdout should not
    have to handle a second copy it did not ask for."""

    respx.get(f"{JIRA_URL}/rest/api/2/issue/DEMO600-1").mock(
        return_value=httpx.Response(200, json=_issue(READ_SAFE_WRITE_UNSAFE["panel"]))
    )
    result = runner.invoke(
        app,
        ["--format", "json", "jira", "issue", "get", "DEMO600-1", "--body-repr", "md"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "body_would_change" not in result.stderr


def test_a_dropped_user_mention_makes_the_body_incomplete_to_read() -> None:
    """Measured on the real converter, and the reason this is worth a test.

    `[~alice] please review` reads as ` please review`. The person is gone. It
    was reported as a warning rather than a loss, so this projection called the
    body COMPLETE -- an agent summarising the issue would never mention them,
    and nothing in the payload said why.

    `write_back_safe` was already false, which is a different question: it says
    republishing would change the issue. Reading and writing are separate
    verdicts here precisely so one cannot stand in for the other, and only the
    reading one tells a summariser to go and look at the raw markup.
    """

    import cfxmark

    wiki = "h2. Title\n\n[~alice] please review this\n"
    result = cfxmark.from_jira_wiki(wiki)
    report = assess_jira_read(
        wiki,
        result.markdown or "",
        document=getattr(result, "document", None),
        losses=tuple(result.losses or ()),
    )

    assert "[~alice]" not in (result.markdown or "")
    assert report.content_complete is False
    assert report.reason == CONTENT_INCOMPLETE
