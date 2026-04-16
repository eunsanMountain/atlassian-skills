"""L2 token schema benchmarks: skill files must be <= 2000 tokens."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.benchmarks.scenarios import count_tokens

ASSETS_DIR = Path(__file__).parent.parent.parent / "src" / "atlassian_skills" / "_assets"

pytestmark = pytest.mark.benchmark


def test_codex_skill_under_2000_tokens() -> None:
    """L2: Codex SKILL.md must be <= 2000 tokens."""
    skill_path = ASSETS_DIR / "codex" / "SKILL.md"
    if not skill_path.exists():
        pytest.skip("Codex skill not yet created")
    content = skill_path.read_text(encoding="utf-8")
    tokens = count_tokens(content)
    print(f"\nL2 Codex SKILL.md: {tokens} tokens")
    assert tokens <= 2000, f"Codex skill: {tokens} tokens > 2000 limit"


def test_claude_skill_under_2000_tokens() -> None:
    """L2: Claude atls.md must be <= 2000 tokens."""
    skill_path = ASSETS_DIR / "claude" / "atls.md"
    if not skill_path.exists():
        pytest.skip("Claude skill not yet created")
    content = skill_path.read_text(encoding="utf-8")
    tokens = count_tokens(content)
    print(f"\nL2 Claude atls.md: {tokens} tokens")
    assert tokens <= 2000, f"Claude skill: {tokens} tokens > 2000 limit"
