from __future__ import annotations

import os
from typing import TypeVar
from uuid import uuid4

import pytest

from atlassian_skills.confluence.client import ConfluenceClient
from atlassian_skills.core.auth import Credential, resolve_credential
from atlassian_skills.core.config import get_profile, load_config
from atlassian_skills.core.errors import AuthError
from atlassian_skills.jira.client import JiraClient

_ClientT = TypeVar("_ClientT")


def _resolve_e2e_client(product: str, client_cls: type[_ClientT]) -> _ClientT:
    """Build an e2e client.

    Resolution order:
      1. Dedicated e2e env vars ``ATLS_E2E_<PRODUCT>_URL`` + ``ATLS_E2E_<PRODUCT>_TOKEN``
         (CI-friendly, no keyring needed).
      2. An explicitly selected ``ATLS_E2E_PROFILE`` via ``resolve_credential``. This
         can reuse credentials saved by ``atls setup`` without exporting plaintext tokens.

    A configured default profile alone never opts into live-server tests.
    """
    up = product.upper()
    env_url = os.environ.get(f"ATLS_E2E_{up}_URL")
    env_token = os.environ.get(f"ATLS_E2E_{up}_TOKEN")

    # 1) explicit e2e env vars — direct PAT, bypasses config/keyring
    if env_url and env_token:
        return client_cls(base_url=env_url.rstrip("/"), credential=Credential(method="pat", token=env_token))

    # 2) reuse a profile only after explicit e2e opt-in
    profile_name = os.environ.get("ATLS_E2E_PROFILE")
    if not profile_name:
        pytest.skip(
            f"Live {product} e2e is not enabled. Set ATLS_E2E_{up}_URL and "
            f"ATLS_E2E_{up}_TOKEN, or explicitly set ATLS_E2E_PROFILE."
        )
    profile = get_profile(load_config(), profile_name)
    resolved_url = env_url or getattr(profile, f"{product}_url", None)
    if not resolved_url:
        pytest.skip(
            f"No {product} URL for e2e. Set ATLS_E2E_{up}_URL, or configure the "
            f"{profile_name!r} profile via 'atls setup'."
        )
    try:
        credential = resolve_credential(profile_name, product, profile, cli_token=env_token)
    except AuthError as exc:
        pytest.skip(f"No {product} credential (env or keyring) for profile {profile_name!r}: {exc}")
    return client_cls(base_url=str(resolved_url).rstrip("/"), credential=credential)


@pytest.fixture
def e2e_jira_client() -> JiraClient:
    return _resolve_e2e_client("jira", JiraClient)


@pytest.fixture
def e2e_confluence_client() -> ConfluenceClient:
    return _resolve_e2e_client("confluence", ConfluenceClient)


@pytest.fixture
def e2e_test_project() -> str:
    project = os.environ.get("ATLS_E2E_PROJECT")
    if not project:
        pytest.skip("Set ATLS_E2E_PROJECT to an explicitly approved test project.")
    return project


@pytest.fixture
def e2e_test_space() -> str:
    space = os.environ.get("ATLS_E2E_SPACE")
    if not space:
        pytest.skip("Set ATLS_E2E_SPACE to an explicitly approved test space.")
    return space


@pytest.fixture
def e2e_allow_writes() -> None:
    """Require an explicit one-shot opt-in before any live-server mutation."""
    if os.environ.get("ATLS_E2E_ALLOW_WRITES") != "1":
        pytest.skip("Set ATLS_E2E_ALLOW_WRITES=1 to run live write tests.")


@pytest.fixture
def e2e_test_parent() -> str | None:
    """Optional parent page id; new e2e pages are created under it when set."""
    return os.environ.get("ATLS_E2E_PARENT") or None


@pytest.fixture
def e2e_temp_page(e2e_allow_writes, e2e_confluence_client, e2e_test_space, e2e_test_parent):
    """Create a throwaway page (under ATLS_E2E_PARENT if set) and delete it on teardown.

    Write-oriented tests must act on their OWN page — never on a pre-existing one —
    so the suite never mutates real content or notifies anyone.
    """
    created = e2e_confluence_client.create_page(
        space_key=e2e_test_space,
        title=f"[atlassian-skills e2e] temp page {uuid4().hex[:12]} (safe to delete)",
        body="<p>temporary e2e fixture page</p>",
        ancestor_id=e2e_test_parent,
    )
    page_id = created.get("id")
    assert page_id, f"fixture could not create a page: {created}"
    try:
        yield page_id
    finally:
        e2e_confluence_client.delete_page(page_id)


@pytest.fixture
def e2e_temp_issue(e2e_allow_writes, e2e_jira_client, e2e_test_project):
    """Create a throwaway issue and delete it on teardown.

    Write-oriented tests must act on their OWN issue — never on a pre-existing one —
    so the suite never comments on / edits someone else's issue (which would notify them).
    """
    fields: dict = {
        "project": {"key": e2e_test_project},
        "summary": f"[atlassian-skills e2e] temp issue {uuid4().hex[:12]} (safe to delete)",
        "issuetype": {"name": "Task"},
    }
    assignee = os.environ.get("ATLS_E2E_ASSIGNEE") or e2e_jira_client.get_myself().name
    assert assignee, "The e2e issue must be assigned to the authenticated test user."
    fields["assignee"] = {"name": assignee}
    created = e2e_jira_client.create_issue(fields=fields)
    key = created.get("key") or created.get("id")
    assert key, f"fixture could not create an issue: {created}"
    try:
        yield key
    finally:
        e2e_jira_client.delete_issue(key)
