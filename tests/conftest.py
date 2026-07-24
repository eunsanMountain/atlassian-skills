from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from atlassian_skills.core.auth import Credential

FIXTURES_ROOT = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True, scope="session")
def _uncoloured_cli_output() -> Iterator[None]:
    """CLI assertions target help *content*; colour is presentation.

    CI runners export FORCE_COLOR, which makes rich interleave escape codes
    inside option names — `"--output" in line` then fails on output a human
    reads as containing `--output`. Pin the whole session to plain text so a
    help assertion means the same thing everywhere.
    """
    saved = {name: os.environ.get(name) for name in ("FORCE_COLOR", "NO_COLOR", "TERM")}
    os.environ.pop("FORCE_COLOR", None)
    os.environ["NO_COLOR"] = "1"
    os.environ["TERM"] = "dumb"
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


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
