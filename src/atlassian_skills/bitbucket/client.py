from __future__ import annotations

from typing import Any

from atlassian_skills.bitbucket.models import (
    Branch,
    BuildStatus,
    Commit,
    DiffStat,
    Project,
    PullRequest,
    PullRequestActivity,
    PullRequestComment,
    PullRequestParticipant,
    Repository,
    Task,
)
from atlassian_skills.core.auth import Credential
from atlassian_skills.core.client import BaseClient
from atlassian_skills.core.errors import NotFoundError, ValidationError


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
        self._current_user_slug: str | None = None

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def _get_paged(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Fetch all pages from a Bitbucket Server paginated endpoint."""
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
    # Current user
    # ------------------------------------------------------------------

    def _get_current_user_slug(self) -> str:
        """Get the authenticated user's slug (cached after first call).

        Tries X-AUSERNAME header from any authenticated request first,
        then falls back to /plugins/servlet/applinks/whoami.
        """
        if self._current_user_slug is not None:
            return self._current_user_slug
        # Primary: X-AUSERNAME header from a lightweight API call
        resp = self.get(f"{self.API}/users", params={"limit": 1})
        slug = resp.headers.get("X-AUSERNAME", "").strip()
        if not slug:
            # Fallback: whoami servlet
            resp2 = self.get("/plugins/servlet/applinks/whoami")
            slug = resp2.text.strip()
        if not slug:
            raise ValidationError("Could not determine current user. Check authentication.")
        self._current_user_slug = str(slug)
        return self._current_user_slug

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

    # ------------------------------------------------------------------
    # Pull Requests — Read (Phase 1)
    # ------------------------------------------------------------------

    def _pr_path(self, project: str, repo: str) -> str:
        return f"/projects/{project}/repos/{repo}/pull-requests"

    def list_pull_requests(
        self,
        project: str,
        repo: str,
        *,
        state: str | None = None,
        limit: int = 25,
    ) -> list[PullRequest]:
        """GET .../pull-requests"""
        params: dict[str, Any] = {}
        if state:
            params["state"] = state.upper()
        items = self._get_paged(self._pr_path(project, repo), params=params, limit=limit)
        return [PullRequest.model_validate(i) for i in items]

    def get_pull_request(self, project: str, repo: str, pr_id: int) -> PullRequest:
        """GET .../pull-requests/{id}"""
        data = self.get(f"{self.API}{self._pr_path(project, repo)}/{pr_id}").json()
        return PullRequest.model_validate(data)

    def get_pull_request_diff(
        self,
        project: str,
        repo: str,
        pr_id: int,
        *,
        path: str | None = None,
        context_lines: int | None = None,
    ) -> str:
        """GET .../pull-requests/{id}/diff — returns raw unified diff text."""
        url = f"{self.API}{self._pr_path(project, repo)}/{pr_id}/diff"
        if path:
            url = f"{url}/{path}"
        params: dict[str, Any] = {}
        if context_lines is not None:
            params["contextLines"] = context_lines
        resp = self.request("GET", url, params=params, headers={"Accept": "text/plain"})
        return resp.text

    def list_pull_request_comments(
        self, project: str, repo: str, pr_id: int, *, limit: int = 25
    ) -> list[PullRequestComment]:
        """Extract comments from PR activities.

        Bitbucket Server's /comments endpoint requires a path parameter.
        Instead, we use /activities and filter for COMMENTED actions.
        """
        items = self._get_paged(
            f"{self._pr_path(project, repo)}/{pr_id}/activities",
            limit=limit,
        )
        comments: list[PullRequestComment] = []
        for item in items:
            if item.get("action") == "COMMENTED" and item.get("comment"):
                comments.append(PullRequestComment.model_validate(item["comment"]))
        return comments

    def list_pull_request_commits(self, project: str, repo: str, pr_id: int, *, limit: int = 25) -> list[Commit]:
        """GET .../pull-requests/{id}/commits"""
        items = self._get_paged(
            f"{self._pr_path(project, repo)}/{pr_id}/commits",
            limit=limit,
        )
        return [Commit.model_validate(i) for i in items]

    def list_pull_request_activities(
        self, project: str, repo: str, pr_id: int, *, limit: int = 25
    ) -> list[PullRequestActivity]:
        """GET .../pull-requests/{id}/activities"""
        items = self._get_paged(
            f"{self._pr_path(project, repo)}/{pr_id}/activities",
            limit=limit,
        )
        return [PullRequestActivity.model_validate(i) for i in items]

    # ------------------------------------------------------------------
    # Branches (Phase 1)
    # ------------------------------------------------------------------

    def list_branches(
        self,
        project: str,
        repo: str,
        *,
        filter_text: str | None = None,
        limit: int = 25,
    ) -> list[Branch]:
        """GET .../branches"""
        params: dict[str, Any] = {}
        if filter_text:
            params["filterText"] = filter_text
        items = self._get_paged(f"/projects/{project}/repos/{repo}/branches", params=params, limit=limit)
        return [Branch.model_validate(i) for i in items]

    # ------------------------------------------------------------------
    # File content (Phase 1)
    # ------------------------------------------------------------------

    def get_file_content(self, project: str, repo: str, path: str, *, at: str | None = None) -> str:
        """GET .../raw/{path} — returns raw file content (byte-preserving)."""
        params: dict[str, Any] = {}
        if at:
            params["at"] = at
        resp = self.get(f"{self.API}/projects/{project}/repos/{repo}/raw/{path}", params=params)
        content_type = resp.headers.get("content-type", "")
        if "text" not in content_type and "json" not in content_type and "xml" not in content_type:
            raise ValidationError("Binary file cannot be displayed as text")
        return resp.text

    # ------------------------------------------------------------------
    # Pull Requests — Write (Phase 2)
    # ------------------------------------------------------------------

    def create_pull_request(
        self,
        project: str,
        repo: str,
        *,
        title: str,
        from_ref: str,
        to_ref: str,
        description: str | None = None,
        reviewers: list[str] | None = None,
    ) -> PullRequest:
        """POST .../pull-requests"""
        payload: dict[str, Any] = {
            "title": title,
            "fromRef": {"id": f"refs/heads/{from_ref}"},
            "toRef": {"id": f"refs/heads/{to_ref}"},
        }
        if description:
            payload["description"] = description
        if reviewers:
            payload["reviewers"] = [{"user": {"name": r}} for r in reviewers]
        data = self.post(f"{self.API}{self._pr_path(project, repo)}", json=payload).json()
        return PullRequest.model_validate(data)

    def update_pull_request(
        self,
        project: str,
        repo: str,
        pr_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        reviewers: list[str] | None = None,
        version: int | None = None,
    ) -> PullRequest:
        """PUT .../pull-requests/{id}"""
        pr = self.get_pull_request(project, repo, pr_id)
        if version is None:
            version = pr.version
        payload: dict[str, Any] = {
            "version": version,
            "title": title or pr.title,
        }
        if description is not None:
            payload["description"] = description
        if reviewers is not None:
            payload["reviewers"] = [{"user": {"name": r}} for r in reviewers]
        data = self.put(f"{self.API}{self._pr_path(project, repo)}/{pr_id}", json=payload).json()
        return PullRequest.model_validate(data)

    def merge_pull_request(
        self,
        project: str,
        repo: str,
        pr_id: int,
        *,
        version: int | None = None,
        strategy: str | None = None,
    ) -> PullRequest:
        """POST .../pull-requests/{id}/merge"""
        if version is None:
            version = self.get_pull_request(project, repo, pr_id).version
        params: dict[str, Any] = {"version": version}
        if strategy:
            params["strategyId"] = strategy
        data = self.post(f"{self.API}{self._pr_path(project, repo)}/{pr_id}/merge", json=params).json()
        return PullRequest.model_validate(data)

    def decline_pull_request(
        self,
        project: str,
        repo: str,
        pr_id: int,
        *,
        version: int | None = None,
    ) -> PullRequest:
        """POST .../pull-requests/{id}/decline"""
        if version is None:
            version = self.get_pull_request(project, repo, pr_id).version
        data = self.post(
            f"{self.API}{self._pr_path(project, repo)}/{pr_id}/decline",
            json={"version": version},
        ).json()
        return PullRequest.model_validate(data)

    def approve_pull_request(self, project: str, repo: str, pr_id: int) -> PullRequestParticipant:
        """POST .../pull-requests/{id}/approve"""
        data = self.post(f"{self.API}{self._pr_path(project, repo)}/{pr_id}/approve").json()
        return PullRequestParticipant.model_validate(data)

    def unapprove_pull_request(self, project: str, repo: str, pr_id: int) -> None:
        """DELETE .../pull-requests/{id}/approve"""
        self.delete(f"{self.API}{self._pr_path(project, repo)}/{pr_id}/approve")

    def needs_work_pull_request(self, project: str, repo: str, pr_id: int) -> None:
        """PUT reviewer status to NEEDS_WORK for the current user."""
        slug = self._get_current_user_slug()
        self.put(
            f"{self.API}{self._pr_path(project, repo)}/{pr_id}/participants/{slug}",
            json={"status": "NEEDS_WORK"},
        )

    def reopen_pull_request(
        self,
        project: str,
        repo: str,
        pr_id: int,
        *,
        version: int | None = None,
    ) -> PullRequest:
        """POST .../pull-requests/{id}/reopen"""
        if version is None:
            version = self.get_pull_request(project, repo, pr_id).version
        data = self.post(
            f"{self.API}{self._pr_path(project, repo)}/{pr_id}/reopen",
            json={"version": version},
        ).json()
        return PullRequest.model_validate(data)

    # ------------------------------------------------------------------
    # Comments — Write (Phase 2)
    # ------------------------------------------------------------------

    def add_pull_request_comment(
        self,
        project: str,
        repo: str,
        pr_id: int,
        *,
        text: str,
        anchor: dict[str, Any] | None = None,
    ) -> PullRequestComment:
        """POST .../pull-requests/{id}/comments"""
        payload: dict[str, Any] = {"text": text}
        if anchor:
            payload["anchor"] = anchor
        data = self.post(
            f"{self.API}{self._pr_path(project, repo)}/{pr_id}/comments",
            json=payload,
        ).json()
        return PullRequestComment.model_validate(data)

    def reply_to_comment(
        self,
        project: str,
        repo: str,
        pr_id: int,
        comment_id: int,
        *,
        text: str,
    ) -> PullRequestComment:
        """POST .../pull-requests/{id}/comments with parent."""
        payload: dict[str, Any] = {
            "text": text,
            "parent": {"id": comment_id},
        }
        data = self.post(
            f"{self.API}{self._pr_path(project, repo)}/{pr_id}/comments",
            json=payload,
        ).json()
        return PullRequestComment.model_validate(data)

    # ------------------------------------------------------------------
    # Comments — CRUD (Phase 3)
    # ------------------------------------------------------------------

    def _get_comment(self, project: str, repo: str, pr_id: int, comment_id: int) -> dict[str, Any]:
        """Fetch a single comment (for version/text auto-fetch)."""
        data: dict[str, Any] = self.get(
            f"{self.API}{self._pr_path(project, repo)}/{pr_id}/comments/{comment_id}"
        ).json()
        return data

    def update_comment(
        self,
        project: str,
        repo: str,
        pr_id: int,
        comment_id: int,
        *,
        text: str,
        version: int | None = None,
    ) -> PullRequestComment:
        """PUT .../comments/{id} — requires full text + version."""
        if version is None:
            current = self._get_comment(project, repo, pr_id, comment_id)
            version = current.get("version", 0)
        payload: dict[str, Any] = {"text": text, "version": version}
        data = self.put(
            f"{self.API}{self._pr_path(project, repo)}/{pr_id}/comments/{comment_id}",
            json=payload,
        ).json()
        return PullRequestComment.model_validate(data)

    def delete_comment(
        self,
        project: str,
        repo: str,
        pr_id: int,
        comment_id: int,
        *,
        version: int | None = None,
    ) -> None:
        """DELETE .../comments/{id}?version=N"""
        if version is None:
            current = self._get_comment(project, repo, pr_id, comment_id)
            version = current.get("version", 0)
        self.delete(
            f"{self.API}{self._pr_path(project, repo)}/{pr_id}/comments/{comment_id}",
            params={"version": version},
        )

    def resolve_comment(
        self,
        project: str,
        repo: str,
        pr_id: int,
        comment_id: int,
        *,
        version: int | None = None,
    ) -> PullRequestComment:
        """PUT .../comments/{id} with state=RESOLVED — requires full text+version."""
        current = self._get_comment(project, repo, pr_id, comment_id)
        if version is None:
            version = current.get("version", 0)
        payload: dict[str, Any] = {
            "text": current.get("text", ""),
            "version": version,
            "state": "RESOLVED",
        }
        data = self.put(
            f"{self.API}{self._pr_path(project, repo)}/{pr_id}/comments/{comment_id}",
            json=payload,
        ).json()
        return PullRequestComment.model_validate(data)

    def reopen_comment(
        self,
        project: str,
        repo: str,
        pr_id: int,
        comment_id: int,
        *,
        version: int | None = None,
    ) -> PullRequestComment:
        """PUT .../comments/{id} with state=OPEN — requires full text+version."""
        current = self._get_comment(project, repo, pr_id, comment_id)
        if version is None:
            version = current.get("version", 0)
        payload: dict[str, Any] = {
            "text": current.get("text", ""),
            "version": version,
            "state": "OPEN",
        }
        data = self.put(
            f"{self.API}{self._pr_path(project, repo)}/{pr_id}/comments/{comment_id}",
            json=payload,
        ).json()
        return PullRequestComment.model_validate(data)

    # ------------------------------------------------------------------
    # Diff stat (Phase 3)
    # ------------------------------------------------------------------

    def get_pull_request_diffstat(self, project: str, repo: str, pr_id: int, *, limit: int = 100) -> list[DiffStat]:
        """GET .../pull-requests/{id}/changes — file-level change stats."""
        items = self._get_paged(
            f"{self._pr_path(project, repo)}/{pr_id}/changes",
            limit=limit,
        )
        return [DiffStat.model_validate(i) for i in items]

    # ------------------------------------------------------------------
    # Build status (Phase 3)
    # ------------------------------------------------------------------

    def get_build_statuses(self, commit_hash: str, *, limit: int = 25) -> list[BuildStatus]:
        """GET /rest/build-status/1.0/commits/{hash} — different API base."""
        items: list[dict[str, Any]] = []
        params: dict[str, Any] = {"limit": limit}
        while True:
            resp = self.get(f"/rest/build-status/1.0/commits/{commit_hash}", params=params)
            data = resp.json()
            items.extend(data.get("values", []))
            if data.get("isLastPage", True):
                break
            next_start = data.get("nextPageStart")
            if next_start is None:
                break
            params["start"] = next_start
        return [BuildStatus.model_validate(i) for i in items]

    # ------------------------------------------------------------------
    # Pending review (Phase 3)
    # ------------------------------------------------------------------

    def list_pull_requests_for_reviewer(self, *, state: str | None = None, limit: int = 25) -> list[PullRequest]:
        """GET /rest/api/1.0/inbox/pull-requests — PRs where current user is reviewer."""
        params: dict[str, Any] = {"limit": limit}
        if state:
            params["state"] = state.upper()
        try:
            resp = self.get(f"{self.API}/inbox/pull-requests", params=params)
            data = resp.json()
            return [PullRequest.model_validate(i) for i in data.get("values", [])]
        except (NotFoundError, ValidationError):
            # Fallback for older server versions that lack the inbox API
            params_fb: dict[str, Any] = {"limit": limit, "role": "REVIEWER"}
            if state:
                params_fb["state"] = state.upper()
            resp = self.get(f"{self.API}/dashboard/pull-requests", params=params_fb)
            data = resp.json()
            return [PullRequest.model_validate(i) for i in data.get("values", [])]

    # ------------------------------------------------------------------
    # Tasks (Phase 3) — list via PR, CRUD via top-level /tasks
    # ------------------------------------------------------------------

    def list_tasks(self, project: str, repo: str, pr_id: int, *, limit: int = 25) -> list[Task]:
        """GET .../pull-requests/{id}/tasks"""
        items = self._get_paged(
            f"{self._pr_path(project, repo)}/{pr_id}/tasks",
            limit=limit,
        )
        return [Task.model_validate(i) for i in items]

    def create_task(
        self,
        *,
        text: str,
        comment_id: int,
    ) -> Task:
        """POST /rest/api/1.0/tasks — anchored to a comment."""
        payload: dict[str, Any] = {
            "anchor": {"id": comment_id, "type": "COMMENT"},
            "text": text,
        }
        data = self.post(f"{self.API}/tasks", json=payload).json()
        return Task.model_validate(data)

    def update_task(self, task_id: int, *, state: str | None = None, text: str | None = None) -> Task:
        """PUT /rest/api/1.0/tasks/{id}"""
        payload: dict[str, Any] = {}
        if state:
            payload["state"] = state.upper()
        if text:
            payload["text"] = text
        data = self.put(f"{self.API}/tasks/{task_id}", json=payload).json()
        return Task.model_validate(data)

    def delete_task(self, task_id: int) -> None:
        """DELETE /rest/api/1.0/tasks/{id}"""
        self.delete(f"{self.API}/tasks/{task_id}")
