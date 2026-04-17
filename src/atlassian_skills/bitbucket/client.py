from __future__ import annotations

from typing import Any

from atlassian_skills.bitbucket.models import Project, Repository
from atlassian_skills.core.auth import Credential
from atlassian_skills.core.client import BaseClient


class BitbucketClient(BaseClient):
    """Bitbucket Server/DC REST API client.

    API base: /rest/api/1.0
    Pagination: start/limit/nextPageStart/isLastPage (offset-based).
    """

    API = "/rest/api/1.0"

    def __init__(
        self,
        base_url: str,
        credential: Credential,
        timeout: float = 30.0,
        verify: str | bool = True,
    ) -> None:
        super().__init__(base_url, credential, timeout, verify=verify)

    def _get_paged(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Fetch all pages from a Bitbucket Server paginated endpoint.

        Bitbucket Server returns:
          { "values": [...], "start": 0, "limit": 25,
            "isLastPage": false, "nextPageStart": 25 }
        """
        if params is None:
            params = {}
        params["limit"] = limit

        items: list[dict[str, Any]] = []
        while True:
            resp = self.get(f"{self.API}{path}", params=params)
            data = resp.json()
            items.extend(data.get("values", []))
            if data.get("isLastPage", True):
                break
            next_start = data.get("nextPageStart")
            if next_start is None:
                break
            params["start"] = next_start
        return items

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def list_projects(self, *, name: str | None = None, limit: int = 25) -> list[Project]:
        """GET /rest/api/1.0/projects"""
        params: dict[str, Any] = {}
        if name:
            params["name"] = name
        items = self._get_paged("/projects", params=params, limit=limit)
        return [Project.model_validate(i) for i in items]

    def get_project(self, key: str) -> Project:
        """GET /rest/api/1.0/projects/{projectKey}"""
        data = self.get(f"{self.API}/projects/{key}").json()
        return Project.model_validate(data)

    # ------------------------------------------------------------------
    # Repositories
    # ------------------------------------------------------------------

    def list_repos(self, project_key: str, *, limit: int = 25) -> list[Repository]:
        """GET /rest/api/1.0/projects/{projectKey}/repos"""
        items = self._get_paged(f"/projects/{project_key}/repos", limit=limit)
        return [Repository.model_validate(i) for i in items]

    def get_repo(self, project_key: str, slug: str) -> Repository:
        """GET /rest/api/1.0/projects/{projectKey}/repos/{repoSlug}"""
        data = self.get(f"{self.API}/projects/{project_key}/repos/{slug}").json()
        return Repository.model_validate(data)
