from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlassian_skills.core.auth import Credential

FIXTURES_ROOT = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_path() -> object:
    def _fixture_path(product: str, name: str) -> Path:
        return FIXTURES_ROOT / product / name

    return _fixture_path


@pytest.fixture
def fixture_json() -> object:
    def _fixture_json(product: str, name: str) -> dict:
        return json.loads((FIXTURES_ROOT / product / name).read_text())

    return _fixture_json


@pytest.fixture(scope="session")
def jira_base_url() -> str:
    return "https://jira.example.com"


@pytest.fixture(scope="session")
def confluence_base_url() -> str:
    return "https://confluence.example.com"


@pytest.fixture(scope="session")
def mock_credential() -> Credential:
    return Credential(method="pat", token="test-token")


@pytest.fixture(scope="session")
def mock_basic_credential() -> Credential:
    return Credential(method="basic", token="test-password", username="test-user")
