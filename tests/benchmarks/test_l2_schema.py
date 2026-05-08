"""L2 token schema benchmarks: skill files must be <= 2000 tokens."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.benchmarks.scenarios import count_tokens

ASSETS_DIR = Path(__file__).parent.parent.parent / "src" / "atlassian_skills" / "_assets"

pytestmark = pytest.mark.benchmark


def test_canonical_skill_under_2000_tokens() -> None:
    """L2: The canonical SKILL.md (shared by Claude + Codex) must be <= 2000 tokens."""
    skill_path = ASSETS_DIR / "skills" / "atls" / "SKILL.md"
    assert skill_path.exists(), f"Canonical skill not found at {skill_path}"
    content = skill_path.read_text(encoding="utf-8")
    tokens = count_tokens(content)
    print(f"\nL2 canonical SKILL.md: {tokens} tokens")
    assert tokens <= 2000, f"Canonical skill: {tokens} tokens > 2000 limit"
