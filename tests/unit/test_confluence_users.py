from __future__ import annotations

import httpx
import pytest
import respx

from atlassian_skills.confluence.client import ConfluenceClient
from atlassian_skills.core.auth import Credential
from atlassian_skills.core.errors import AuthError, ForbiddenError
from atlassian_skills.jira.models import User

BASE_URL = "https://confluence.example.com"

_USER_FIXTURES = [
    {
        "displayName": "Alice Kim",
        "name": "alice.kim",
        "emailAddress": "alice.kim@corp.com",
        "key": "U001",
    },
    {
        "displayName": "Bob Lee",
        "name": "bob.lee",
        "emailAddress": "bob.lee@corp.com",
        "key": "U002",
    },
    {
        "displayName": "Charlie Alice",
        "name": "charlie",
        "emailAddress": "charlie@corp.com",
        "key": "U003",
    },
    {
        "displayName": "Diana Park",
        "name": "diana.park",
        "emailAddress": "diana.park@corp.com",
        "key": "U004",
    },
]


@pytest.fixture
def cred() -> Credential:
    return Credential(method="pat", token="test-token")


@pytest.fixture
def client(cred: Credential) -> ConfluenceClient:
    return ConfluenceClient(BASE_URL, cred)


def _group_fixture(users: list[dict] | None = None) -> dict:
    """Build a group member response payload."""
    return {
        "results": users if users is not None else _USER_FIXTURES,
        "start": 0,
        "limit": 200,
        "size": len(users if users is not None else _USER_FIXTURES),
        "_links": {},
    }


# ---------------------------------------------------------------------------
# search_users — matching
# ---------------------------------------------------------------------------


@respx.mock
def test_search_users_returns_matching_users(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/group/confluence-users/member").mock(
        return_value=httpx.Response(200, json=_group_fixture())
    )

    users = client.search_users("alice")

    assert len(users) == 2
    assert all(isinstance(u, User) for u in users)
    names = {u.name for u in users}
    assert "alice.kim" in names
    assert "charlie" in names  # displayName "Charlie Alice" contains "alice"


@respx.mock
def test_search_users_exact_username(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/group/confluence-users/member").mock(
        return_value=httpx.Response(200, json=_group_fixture())
    )

    users = client.search_users("bob.lee")

    assert len(users) == 1
    assert users[0].name == "bob.lee"


@respx.mock
def test_search_users_by_email(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/group/confluence-users/member").mock(
        return_value=httpx.Response(200, json=_group_fixture())
    )

    users = client.search_users("diana.park@corp")

    assert len(users) == 1
    assert users[0].name == "diana.park"


@respx.mock
def test_search_users_no_match(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/group/confluence-users/member").mock(
        return_value=httpx.Response(200, json=_group_fixture())
    )

    users = client.search_users("zzz-nomatch")

    assert users == []


@respx.mock
def test_search_users_empty_query_returns_all(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/group/confluence-users/member").mock(
        return_value=httpx.Response(200, json=_group_fixture())
    )

    users = client.search_users("")

    assert len(users) == len(_USER_FIXTURES)


@respx.mock
def test_search_users_case_insensitive(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/group/confluence-users/member").mock(
        return_value=httpx.Response(200, json=_group_fixture())
    )

    users_lower = client.search_users("alice")
    users_upper = client.search_users("ALICE")

    assert len(users_lower) == len(users_upper)
    assert {u.name for u in users_lower} == {u.name for u in users_upper}


@respx.mock
def test_search_users_fuzzy_display_name(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/group/confluence-users/member").mock(
        return_value=httpx.Response(200, json=_group_fixture())
    )

    users = client.search_users("park")

    assert len(users) == 1
    assert users[0].name == "diana.park"


@respx.mock
def test_search_users_empty_group(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/group/confluence-users/member").mock(
        return_value=httpx.Response(200, json=_group_fixture(users=[]))
    )

    users = client.search_users("alice")

    assert users == []


# ---------------------------------------------------------------------------
# search_users — HTTP error handling
# ---------------------------------------------------------------------------


@respx.mock
def test_search_users_401_raises_auth_error(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/group/confluence-users/member").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )

    with pytest.raises(AuthError):
        client.search_users("alice")


@respx.mock
def test_search_users_403_raises_permission_error(client: ConfluenceClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/group/confluence-users/member").mock(
        return_value=httpx.Response(403, text="Forbidden — group access denied")
    )

    with pytest.raises(ForbiddenError):
        client.search_users("bob")


# ---------------------------------------------------------------------------
# search_users — custom group
# ---------------------------------------------------------------------------


@respx.mock
def test_search_users_custom_group(client: ConfluenceClient) -> None:
    custom_group_users = [
        {"displayName": "Admin User", "name": "admin", "emailAddress": "admin@corp.com", "key": "A001"},
    ]
    respx.get(f"{BASE_URL}/rest/api/group/admins/member").mock(
        return_value=httpx.Response(200, json=_group_fixture(users=custom_group_users))
    )

    users = client.search_users("admin", group_name="admins")

    assert len(users) == 1
    assert users[0].name == "admin"
