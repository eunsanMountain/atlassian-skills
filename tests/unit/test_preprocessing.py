from __future__ import annotations

import pytest

from atlassian_skills.jira.preprocessing import normalize_smart_links, preprocess_jira_text, replace_mentions

# ---------------------------------------------------------------------------
# replace_mentions
# ---------------------------------------------------------------------------


def test_replace_mentions_single() -> None:
    result = replace_mentions("[~accountid:abc123]")
    assert result == "@user-abc123"


def test_replace_mentions_multiple() -> None:
    text = "Hello [~accountid:abc123] and [~accountid:xyz789]!"
    result = replace_mentions(text)
    assert result == "Hello @user-abc123 and @user-xyz789!"


def test_replace_mentions_none() -> None:
    text = "No mentions here, just plain text."
    assert replace_mentions(text) == text


@pytest.mark.parametrize(
    "account_id,expected",
    [
        ("user.name", "@user-user.name"),
        ("user-name", "@user-user-name"),
        ("user_name", "@user-user_name"),
        ("user.name-foo_bar", "@user-user.name-foo_bar"),
    ],
)
def test_replace_mentions_special_chars_in_id(account_id: str, expected: str) -> None:
    result = replace_mentions(f"[~accountid:{account_id}]")
    assert result == expected


def test_replace_mentions_at_start_of_text() -> None:
    result = replace_mentions("[~accountid:abc123] said hello")
    assert result == "@user-abc123 said hello"


def test_replace_mentions_at_end_of_text() -> None:
    result = replace_mentions("Hello from [~accountid:abc123]")
    assert result == "Hello from @user-abc123"


def test_replace_mentions_empty_string() -> None:
    assert replace_mentions("") == ""


def test_replace_mentions_nested_bracket_edge_case() -> None:
    # Outer brackets not matching accountid format should be left alone
    text = "[[~accountid:abc123]]"
    result = replace_mentions(text)
    # The inner match is replaced; the outer brackets remain
    assert "@user-abc123" in result


def test_replace_mentions_in_code_block_context() -> None:
    text = "{code}[~accountid:abc123]{code}"
    result = replace_mentions(text)
    assert result == "{code}@user-abc123{code}"


def test_replace_mentions_old_style_username_not_converted() -> None:
    # [~username] format (without accountid:) must NOT be converted
    text = "[~someuser]"
    assert replace_mentions(text) == text


# ---------------------------------------------------------------------------
# normalize_smart_links
# ---------------------------------------------------------------------------


def test_normalize_smart_links_basic() -> None:
    result = normalize_smart_links("[Google|https://google.com|smart-link]")
    assert result == "[Google|https://google.com]"


def test_normalize_smart_links_multiple() -> None:
    text = "[Google|https://google.com|smart-link] and [GitHub|https://github.com|smart-link]"
    result = normalize_smart_links(text)
    assert result == "[Google|https://google.com] and [GitHub|https://github.com]"


def test_normalize_smart_links_none() -> None:
    text = "No smart links here."
    assert normalize_smart_links(text) == text


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("[text|https://example.com|SMART-LINK]", "[text|https://example.com]"),
        ("[text|https://example.com|Smart-Link]", "[text|https://example.com]"),
        ("[text|https://example.com|smart-link]", "[text|https://example.com]"),
        ("[text|https://example.com|SMART-link]", "[text|https://example.com]"),
    ],
)
def test_normalize_smart_links_case_insensitive(raw: str, expected: str) -> None:
    assert normalize_smart_links(raw) == expected


def test_normalize_smart_links_regular_jira_link_unchanged() -> None:
    # Two-part Jira links [text|url] must not be touched
    text = "[Google|https://google.com]"
    assert normalize_smart_links(text) == text


def test_normalize_smart_links_special_chars_in_url() -> None:
    url = "https://example.com/path?foo=bar&baz=qux#anchor"
    result = normalize_smart_links(f"[label|{url}|smart-link]")
    assert result == f"[label|{url}]"


def test_normalize_smart_links_empty_string() -> None:
    assert normalize_smart_links("") == ""


# ---------------------------------------------------------------------------
# preprocess_jira_text (combined pipeline)
# ---------------------------------------------------------------------------


def test_preprocess_jira_text_both_replacements() -> None:
    text = "Assigned to [~accountid:u1]. See [Docs|https://docs.example.com|smart-link]."
    result = preprocess_jira_text(text)
    assert "@user-u1" in result
    assert "[Docs|https://docs.example.com]" in result
    assert "[~accountid:" not in result
    assert "smart-link" not in result


def test_preprocess_jira_text_neither() -> None:
    text = "Just plain text with no Jira markup."
    assert preprocess_jira_text(text) == text


def test_preprocess_jira_text_complex_real_world_description() -> None:
    text = (
        "h2. Overview\n"
        "\n"
        "This ticket was reported by [~accountid:reporter.id] and assigned to [~accountid:dev.id].\n"
        "\n"
        "h3. Steps to reproduce\n"
        "# Open the [Dashboard|https://app.example.com/dashboard|smart-link]\n"
        "# Click *Submit*\n"
        "# Observe the error\n"
        "\n"
        "See also the [Design Doc|https://docs.example.com/design|smart-link] for context.\n"
    )
    result = preprocess_jira_text(text)

    assert "@user-reporter.id" in result
    assert "@user-dev.id" in result
    assert "[Dashboard|https://app.example.com/dashboard]" in result
    assert "[Design Doc|https://docs.example.com/design]" in result
    assert "[~accountid:" not in result
    assert "smart-link" not in result
    # Wiki markup must be preserved
    assert "h2. Overview" in result
    assert "*Submit*" in result


def test_preprocess_jira_text_large_text_many_replacements() -> None:
    n = 50
    mentions = " ".join(f"[~accountid:user{i}]" for i in range(n))
    links = " ".join(f"[Link{i}|https://example.com/{i}|smart-link]" for i in range(n))
    text = f"{mentions}\n{links}"
    result = preprocess_jira_text(text)

    for i in range(n):
        assert f"@user-user{i}" in result
        assert f"[Link{i}|https://example.com/{i}]" in result
    assert "[~accountid:" not in result
    assert "smart-link" not in result


def test_preprocess_jira_text_wiki_markup_not_interfered() -> None:
    text = (
        "h1. Title\n"
        "* bullet one\n"
        "* bullet two\n"
        "*bold text*\n"
        "_italic_\n"
        "{{monospace}}\n"
        "{code:python}\nprint('hello')\n{code}\n"
    )
    assert preprocess_jira_text(text) == text


# ---------------------------------------------------------------------------
# replace_mentions — parametrized spec cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "input_text,expected",
    [
        # accountid-style mention (the only format the module converts)
        ("[~accountid:user1]", "@user-user1"),
        # plain username format must NOT be converted (module only handles accountid:)
        ("[~someuser]", "[~someuser]"),
        # no mentions at all
        ("no mentions here", "no mentions here"),
        # empty string
        ("", ""),
        # multiple accountid mentions
        (
            "[~accountid:user1] and [~accountid:user2]",
            "@user-user1 and @user-user2",
        ),
    ],
)
def test_replace_mentions_parametrized(input_text: str, expected: str) -> None:
    assert replace_mentions(input_text) == expected


# ---------------------------------------------------------------------------
# replace_mentions — additional edge cases
# ---------------------------------------------------------------------------


def test_replace_mentions_adjacent_no_space() -> None:
    """Two mentions with no separator between them."""
    result = replace_mentions("[~accountid:a][~accountid:b]")
    assert result == "@user-a@user-b"


def test_replace_mentions_numeric_id() -> None:
    """Purely numeric account IDs are valid."""
    result = replace_mentions("[~accountid:123456]")
    assert result == "@user-123456"


def test_replace_mentions_very_long_id() -> None:
    """Long account IDs (e.g. 128-char GUIDs) should be handled."""
    long_id = "a" * 128
    result = replace_mentions(f"[~accountid:{long_id}]")
    assert result == f"@user-{long_id}"


def test_replace_mentions_multiline_text() -> None:
    """Mentions embedded across a multiline description."""
    text = "Line 1\n[~accountid:u1]\nLine 3"
    result = replace_mentions(text)
    assert result == "Line 1\n@user-u1\nLine 3"


# ---------------------------------------------------------------------------
# normalize_smart_links — additional edge cases
# ---------------------------------------------------------------------------


def test_normalize_smart_links_url_with_spaces_encoded() -> None:
    """URL with percent-encoded spaces should pass through unchanged."""
    url = "https://example.com/path%20with%20spaces"
    result = normalize_smart_links(f"[label|{url}|smart-link]")
    assert result == f"[label|{url}]"


def test_normalize_smart_links_adjacent_no_space() -> None:
    """Two smart-link annotations with no separator."""
    text = "[A|https://a.com|smart-link][B|https://b.com|smart-link]"
    result = normalize_smart_links(text)
    assert result == "[A|https://a.com][B|https://b.com]"


def test_normalize_smart_links_empty_label() -> None:
    """Empty label part does NOT match the regex (requires ≥1 char before first pipe).
    The markup is left unchanged — this documents the known boundary of the regex."""
    text = "[|https://example.com|smart-link]"
    result = normalize_smart_links(text)
    assert result == text


def test_normalize_smart_links_pipe_in_url_not_matched() -> None:
    """A URL containing a pipe character breaks the three-part pattern — must be left alone."""
    # This is an edge case the regex intentionally cannot handle (greedy/non-greedy limits).
    # Verify that malformed markup does not cause an exception.
    text = "[label|https://example.com/a|b|smart-link]"
    result = normalize_smart_links(text)
    # Result may vary; the important invariant is no exception is raised.
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# preprocess_jira_text — additional pipeline edge cases
# ---------------------------------------------------------------------------


def test_preprocess_jira_text_empty_string() -> None:
    assert preprocess_jira_text("") == ""


def test_preprocess_jira_text_mention_and_smart_link_same_word() -> None:
    """Mention immediately followed by a smart-link on the same line."""
    text = "[~accountid:reporter] see [Doc|https://doc.example.com|smart-link]"
    result = preprocess_jira_text(text)
    assert result == "@user-reporter see [Doc|https://doc.example.com]"


def test_preprocess_jira_text_order_independence() -> None:
    """Result must be the same whether smart-link or mention comes first."""
    text_a = "[~accountid:u1] and [Link|https://x.com|smart-link]"
    text_b = "[Link|https://x.com|smart-link] and [~accountid:u1]"
    result_a = preprocess_jira_text(text_a)
    result_b = preprocess_jira_text(text_b)
    assert "@user-u1" in result_a
    assert "[Link|https://x.com]" in result_a
    assert "@user-u1" in result_b
    assert "[Link|https://x.com]" in result_b


def test_preprocess_jira_text_idempotent() -> None:
    """Running preprocess twice must not double-transform."""
    text = "Hello [~accountid:u1], see [Doc|https://doc.example.com|smart-link]"
    once = preprocess_jira_text(text)
    twice = preprocess_jira_text(once)
    assert once == twice
