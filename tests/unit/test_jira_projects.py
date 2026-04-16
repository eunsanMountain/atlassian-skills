from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from atlassian_skills.core.auth import Credential
from atlassian_skills.jira.client import JiraClient
from atlassian_skills.jira.models import (
    Project,
    ProjectComponent,
    ProjectVersion,
    SearchResult,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "jira"
BASE_URL = "https://jira.example.com"


def _load(name: str) -> dict | list:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def cred() -> Credential:
    return Credential(method="pat", token="test-token")


@pytest.fixture
def client(cred: Credential) -> JiraClient:
    return JiraClient(BASE_URL, cred)


# ---------------------------------------------------------------------------
# list_projects
# ---------------------------------------------------------------------------


@respx.mock
def test_list_projects_returns_list(client: JiraClient) -> None:
    fixture = _load("get-projects-sample.json")
    respx.get(f"{BASE_URL}/rest/api/2/project").mock(return_value=httpx.Response(200, json=fixture))

    projects = client.list_projects()

    assert isinstance(projects, list)
    assert len(projects) == 3
    assert all(isinstance(p, Project) for p in projects)
    assert projects[0].key == "TEST"
    assert projects[1].key == "DEMO"
    assert projects[2].key == "SAMPLE"


@respx.mock
def test_list_projects_parses_project_fields(client: JiraClient) -> None:
    # Use snake_case fixture format that the models expect
    payload = [
        {
            "id": "10000",
            "key": "TEST",
            "name": "Test Project",
            "project_type_key": "software",
            "description": "Test project for unit tests",
            "lead": {"displayName": "Test Lead", "name": "test.lead"},
        }
    ]
    respx.get(f"{BASE_URL}/rest/api/2/project").mock(return_value=httpx.Response(200, json=payload))

    projects = client.list_projects()

    p = projects[0]
    assert p.id == "10000"
    assert p.name == "Test Project"
    assert p.project_type_key == "software"
    assert p.description == "Test project for unit tests"
    assert p.lead is not None
    assert p.lead.name == "test.lead"


@respx.mock
def test_list_projects_wrapped_values(client: JiraClient) -> None:
    """list_projects handles paginated wrapper {values: [...]}."""
    payload = {
        "values": [
            {"id": "10000", "key": "PROJ", "name": "A Project"},
        ],
        "maxResults": 50,
        "startAt": 0,
        "total": 1,
    }
    respx.get(f"{BASE_URL}/rest/api/2/project").mock(return_value=httpx.Response(200, json=payload))

    projects = client.list_projects()

    assert len(projects) == 1
    assert projects[0].key == "PROJ"


@respx.mock
def test_list_projects_empty(client: JiraClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/2/project").mock(return_value=httpx.Response(200, json=[]))

    projects = client.list_projects()

    assert projects == []


@respx.mock
def test_list_projects_http_error(client: JiraClient) -> None:
    from atlassian_skills.core.errors import AtlasError

    respx.get(f"{BASE_URL}/rest/api/2/project").mock(
        return_value=httpx.Response(500, json={"message": "Internal Server Error"})
    )

    with pytest.raises(AtlasError):
        client.list_projects()


# ---------------------------------------------------------------------------
# get_project_issues
# ---------------------------------------------------------------------------


@respx.mock
def test_get_project_issues_returns_search_result(client: JiraClient) -> None:
    fixture = _load("search-proj.json")
    respx.get(f"{BASE_URL}/rest/api/2/search").mock(return_value=httpx.Response(200, json=fixture))

    result = client.get_project_issues("PROJ")

    assert isinstance(result, SearchResult)
    assert result.total == 23
    assert len(result.issues) == 3


@respx.mock
def test_get_project_issues_uses_project_jql(client: JiraClient) -> None:
    payload: dict = {"total": 0, "start_at": 0, "max_results": 50, "issues": []}
    route = respx.get(f"{BASE_URL}/rest/api/2/search").mock(return_value=httpx.Response(200, json=payload))

    client.get_project_issues("DEMO")

    called_params = route.calls.last.request.url.params
    assert 'project="DEMO"' in called_params["jql"]


@respx.mock
def test_get_project_issues_with_jql_extra(client: JiraClient) -> None:
    payload: dict = {"total": 5, "start_at": 0, "max_results": 50, "issues": []}
    route = respx.get(f"{BASE_URL}/rest/api/2/search").mock(return_value=httpx.Response(200, json=payload))

    client.get_project_issues("TEST", jql_extra="status=Open")

    called_params = route.calls.last.request.url.params
    assert "status=Open" in called_params["jql"]


@respx.mock
def test_get_project_issues_empty(client: JiraClient) -> None:
    payload: dict = {"total": 0, "start_at": 0, "max_results": 50, "issues": []}
    respx.get(f"{BASE_URL}/rest/api/2/search").mock(return_value=httpx.Response(200, json=payload))

    result = client.get_project_issues("EMPTY")

    assert isinstance(result, SearchResult)
    assert result.total == 0
    assert result.issues == []


@respx.mock
def test_get_project_issues_respects_limit(client: JiraClient) -> None:
    payload: dict = {"total": 100, "start_at": 0, "max_results": 10, "issues": []}
    route = respx.get(f"{BASE_URL}/rest/api/2/search").mock(return_value=httpx.Response(200, json=payload))

    client.get_project_issues("BIG", limit=10)

    called_params = route.calls.last.request.url.params
    assert called_params["maxResults"] == "10"


# ---------------------------------------------------------------------------
# get_project_versions
# ---------------------------------------------------------------------------


@respx.mock
def test_get_project_versions_returns_list(client: JiraClient) -> None:
    payload = [
        {"id": "10000", "name": "1.0", "released": True, "archived": False},
        {"id": "10001", "name": "2.0", "released": False, "archived": False},
    ]
    respx.get(f"{BASE_URL}/rest/api/2/project/TEST/versions").mock(return_value=httpx.Response(200, json=payload))

    versions = client.get_project_versions("TEST")

    assert len(versions) == 2
    assert all(isinstance(v, ProjectVersion) for v in versions)
    assert versions[0].id == "10000"
    assert versions[0].name == "1.0"
    assert versions[0].released is True
    assert versions[1].released is False


@respx.mock
def test_get_project_versions_empty(client: JiraClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/2/project/TEST/versions").mock(return_value=httpx.Response(200, json=[]))

    versions = client.get_project_versions("TEST")

    assert versions == []


@respx.mock
def test_get_project_versions_http_error(client: JiraClient) -> None:
    from atlassian_skills.core.errors import NotFoundError

    respx.get(f"{BASE_URL}/rest/api/2/project/NOPE/versions").mock(return_value=httpx.Response(404, text="Not found"))

    with pytest.raises(NotFoundError):
        client.get_project_versions("NOPE")


@respx.mock
def test_get_project_versions_model_fields(client: JiraClient) -> None:
    payload = [
        {
            "id": "20000",
            "name": "3.0",
            "description": "Major release",
            "released": True,
            "archived": False,
            "release_date": "2024-06-01",
        }
    ]
    respx.get(f"{BASE_URL}/rest/api/2/project/TEST/versions").mock(return_value=httpx.Response(200, json=payload))

    versions = client.get_project_versions("TEST")

    v = versions[0]
    assert v.description == "Major release"
    assert v.release_date == "2024-06-01"


# ---------------------------------------------------------------------------
# get_project_components
# ---------------------------------------------------------------------------


@respx.mock
def test_get_project_components_returns_list(client: JiraClient) -> None:
    payload = [
        {"id": "10000", "name": "Backend"},
        {"id": "10001", "name": "Frontend"},
    ]
    respx.get(f"{BASE_URL}/rest/api/2/project/TEST/components").mock(return_value=httpx.Response(200, json=payload))

    components = client.get_project_components("TEST")

    assert len(components) == 2
    assert all(isinstance(c, ProjectComponent) for c in components)
    assert components[0].name == "Backend"
    assert components[1].name == "Frontend"


@respx.mock
def test_get_project_components_empty(client: JiraClient) -> None:
    respx.get(f"{BASE_URL}/rest/api/2/project/TEST/components").mock(return_value=httpx.Response(200, json=[]))

    components = client.get_project_components("TEST")

    assert components == []


@respx.mock
def test_get_project_components_http_error(client: JiraClient) -> None:
    from atlassian_skills.core.errors import AtlasError

    respx.get(f"{BASE_URL}/rest/api/2/project/NOPE/components").mock(return_value=httpx.Response(403, text="Forbidden"))

    with pytest.raises(AtlasError):
        client.get_project_components("NOPE")


@respx.mock
def test_get_project_components_model_fields(client: JiraClient) -> None:
    payload = [
        {
            "id": "30000",
            "name": "API",
            "description": "REST API layer",
            "lead": {"displayName": "Lead Dev", "name": "lead.dev"},
        }
    ]
    respx.get(f"{BASE_URL}/rest/api/2/project/TEST/components").mock(return_value=httpx.Response(200, json=payload))

    components = client.get_project_components("TEST")

    c = components[0]
    assert c.description == "REST API layer"
    assert c.lead is not None
    assert c.lead.name == "lead.dev"


# ---------------------------------------------------------------------------
# Model parsing verification
# ---------------------------------------------------------------------------


def test_project_model_minimal() -> None:
    data = {"id": "10000", "key": "PROJ", "name": "My Project"}
    p = Project.model_validate(data)
    assert p.key == "PROJ"
    assert p.id == "10000"
    assert p.name == "My Project"
    assert p.description is None
    assert p.lead is None


def test_project_version_model() -> None:
    data = {
        "id": "10001",
        "name": "v1.0",
        "released": True,
        "archived": False,
        "release_date": "2024-03-01",
    }
    v = ProjectVersion.model_validate(data)
    assert v.id == "10001"
    assert v.released is True
    assert v.archived is False
    assert v.release_date == "2024-03-01"


def test_project_component_model() -> None:
    data = {"id": "10002", "name": "DB", "description": "Database layer"}
    c = ProjectComponent.model_validate(data)
    assert c.id == "10002"
    assert c.name == "DB"
    assert c.description == "Database layer"
