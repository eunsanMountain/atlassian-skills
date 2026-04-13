from __future__ import annotations

import re


def replace_mentions(text: str) -> str:
    """[~accountid:X] → @user-X"""
    return re.sub(r"\[~accountid:([^\]]+)\]", r"@user-\1", text)


def normalize_smart_links(text: str) -> str:
    """[text|url|smart-link] → [text|url]"""
    return re.sub(r"\[([^\|\]]+)\|([^\|\]]+)\|smart-link\]", r"[\1|\2]", text, flags=re.IGNORECASE)


def preprocess_jira_text(text: str) -> str:
    """Read 경로 전처리: 서버 wiki markup → cfxmark 호출 전."""
    text = replace_mentions(text)
    text = normalize_smart_links(text)
    return text
