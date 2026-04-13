from __future__ import annotations

from typing import Any

from atlassian_skills.core.auth import Credential
from atlassian_skills.core.client import BaseClient
from atlassian_skills.jira.models import (
    Board,
    Issue,
    JiraAttachment,
    JiraComment,
    JiraField,
    LinkType,
    Project,
    ProjectComponent,
    ProjectVersion,
    SearchResult,
    Sprint,
    Transition,
    User,
    WatcherList,
    WorklogList,
)


class JiraClient(BaseClient):
    def __init__(self, base_url: str, credential: Credential, timeout: float = 30.0, verify: str | bool = True) -> None:
        super().__init__(base_url, credential, timeout, verify=verify)

    # ------------------------------------------------------------------
    # User
    # ------------------------------------------------------------------

    def get_user(self, identifier: str) -> User:
        """Get a Jira user. Auto-detects identifier type.

        - Contains '@' → email: use /user/search (Server/DC doesn't support email in /user)
        - Contains ':' → Cloud accountId (e.g. '557058:abcd...')
        - Starts with 'JIRAUSER' or hex-only → Server/DC user key
        - Otherwise → username
        """
        if "@" in identifier:
            # /rest/api/2/user doesn't accept email; use /user/search
            results = self.get("/rest/api/2/user/search", params={"username": identifier}).json()
            if not results:
                from atlassian_skills.core.errors import NotFoundError
                raise NotFoundError(f"No Jira user matches email '{identifier}'")
            return User.model_validate(results[0])
        params: dict[str, Any]
        if ":" in identifier:
            # Cloud accountId format (e.g. 557058:abcd1234-...)
            params = {"accountId": identifier}
        elif identifier.startswith("JIRAUSER") or (
            len(identifier) > 20 and all(c in "0123456789abcdef" for c in identifier.lower())
        ):
            params = {"key": identifier}
        else:
            params = {"username": identifier}
        data = self.get("/rest/api/2/user", params=params).json()
        return User.model_validate(data)

    def get_myself(self) -> User:
        """GET /rest/api/2/myself — current authenticated user."""
        data = self.get("/rest/api/2/myself").json()
        return User.model_validate(data)

    # ------------------------------------------------------------------
    # Issue read
    # ------------------------------------------------------------------

    def get_issue(
        self,
        key: str,
        fields: list[str] | None = None,
        expand: str | None = None,
    ) -> Issue:
        params: dict[str, Any] = {}
        if fields:
            params["fields"] = ",".join(fields)
        if expand:
            params["expand"] = expand
        data = self.get(f"/rest/api/2/issue/{key}", params=params or None).json()
        return Issue.model_validate(data)

    def get_issue_raw_text(
        self,
        key: str,
        fields: list[str] | None = None,
        expand: str | None = None,
    ) -> str:
        """Return verbatim response text (byte-preserving raw contract)."""
        params: dict[str, Any] = {}
        if fields:
            params["fields"] = ",".join(fields)
        if expand:
            params["expand"] = expand
        return self.get(f"/rest/api/2/issue/{key}", params=params or None).text

    def get_issue_raw(
        self,
        key: str,
        fields: list[str] | None = None,
        expand: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if fields:
            params["fields"] = ",".join(fields)
        if expand:
            params["expand"] = expand
        result: dict[str, Any] = self.get(f"/rest/api/2/issue/{key}", params=params or None).json()
        return result

    _IMAGE_EXTENSIONS: frozenset[str] = frozenset(
        {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico", ".tiff"}
    )

    def get_issue_images(self, key: str) -> list[dict[str, Any]]:
        data = self.get(f"/rest/api/2/issue/{key}", params={"fields": "attachment"}).json()
        attachments: list[dict[str, Any]] = (
            data.get("fields", {}).get("attachment", data.get("attachments", []))
        )
        result: list[dict[str, Any]] = []
        for a in attachments:
            mime = (a.get("mimeType") or a.get("mime_type") or "").lower()
            if mime.startswith("image/"):
                result.append(a)
                continue
            # Extension fallback only when MIME is missing/ambiguous
            # (not when server explicitly reports a non-image MIME like application/pdf)
            if not mime or mime in ("application/octet-stream", "binary/octet-stream"):
                filename = (a.get("filename") or "").lower()
                if any(filename.endswith(ext) for ext in self._IMAGE_EXTENSIONS):
                    result.append(a)
        return result

    def get_issue_dates(self, key: str) -> dict[str, str | None]:
        data: dict[str, Any] = self.get(
            f"/rest/api/2/issue/{key}",
            params={"fields": "duedate,created,updated,resolutiondate"},
        ).json()
        fields = data.get("fields", data)
        return {
            "key": data.get("key", key),
            "created": fields.get("created"),
            "updated": fields.get("updated"),
            "due_date": fields.get("duedate"),
            "resolution_date": fields.get("resolutiondate"),
        }

    def get_issue_sla(self, key: str) -> dict[str, Any]:
        result: dict[str, Any] = self.get(f"/rest/servicedeskapi/request/{key}/sla").json()
        return result

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        jql: str,
        fields: list[str] | None = None,
        start_at: int = 0,
        max_results: int = 50,
    ) -> SearchResult:
        params: dict[str, Any] = {"jql": jql, "startAt": start_at, "maxResults": max_results}
        if fields:
            params["fields"] = ",".join(fields)
        data = self.get("/rest/api/2/search", params=params).json()
        return SearchResult.model_validate(data)

    def get_transitions(self, key: str) -> list[Transition]:
        data = self.get(f"/rest/api/2/issue/{key}/transitions").json()
        # Real API: {"transitions": [...]}; fixture / some endpoints: plain list
        items: list[Any] = data if isinstance(data, list) else data.get("transitions", [])
        return [Transition.model_validate(t) for t in items]

    # ------------------------------------------------------------------
    # Fields
    # ------------------------------------------------------------------

    def search_fields(self, keyword: str | None = None) -> list[JiraField]:
        data = self.get("/rest/api/2/field").json()
        all_fields = [JiraField.model_validate(f) for f in data]
        if keyword:
            kw = keyword.lower()
            return [f for f in all_fields if kw in f.id.lower() or kw in f.name.lower()]
        return all_fields

    def get_field_options(
        self,
        field_id: str,
        project_key: str,
        issue_type: str,
    ) -> list[dict[str, Any]]:
        data = self.get(
            "/rest/api/2/issue/createmeta",
            params={
                "projectKeys": project_key,
                "issuetypeNames": issue_type,
                "expand": "projects.issuetypes.fields",
            },
        ).json()
        for project in data.get("projects", []):
            for it in project.get("issuetypes", []):
                field = it.get("fields", {}).get(field_id, {})
                values: list[dict[str, Any]] = field.get("allowedValues", [])
                return values
        return []

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def list_projects(self) -> list[Project]:
        data = self.get("/rest/api/2/project").json()
        items: list[Any] = data if isinstance(data, list) else data.get("values", [])
        return [Project.model_validate(p) for p in items]

    def get_project_issues(
        self,
        key: str,
        jql_extra: str | None = None,
        limit: int = 50,
    ) -> SearchResult:
        jql = f'project="{key}"'
        if jql_extra:
            jql += f" AND {jql_extra}"
        return self.search(jql, max_results=limit)

    def get_project_versions(self, key: str) -> list[ProjectVersion]:
        data = self.get(f"/rest/api/2/project/{key}/versions").json()
        items: list[Any] = data if isinstance(data, list) else data.get("values", [])
        return [ProjectVersion.model_validate(v) for v in items]

    def get_project_components(self, key: str) -> list[ProjectComponent]:
        data = self.get(f"/rest/api/2/project/{key}/components").json()
        items: list[Any] = data if isinstance(data, list) else data.get("values", [])
        return [ProjectComponent.model_validate(c) for c in items]

    # ------------------------------------------------------------------
    # Agile: boards, sprints
    # ------------------------------------------------------------------

    def list_boards(
        self,
        name: str | None = None,
        project: str | None = None,
        board_type: str | None = None,
    ) -> list[Board]:
        params: dict[str, Any] = {}
        if name:
            params["name"] = name
        if project:
            params["projectKeyOrId"] = project
        if board_type:
            params["type"] = board_type
        data = self.get("/rest/agile/1.0/board", params=params or None).json()
        items: list[Any] = data if isinstance(data, list) else data.get("values", [])
        return [Board.model_validate(b) for b in items]

    def get_board_issues(
        self,
        board_id: int | str,
        jql: str | None = None,
        limit: int = 50,
    ) -> list[Issue]:
        params: dict[str, Any] = {"maxResults": limit}
        if jql:
            params["jql"] = jql
        data = self.get(f"/rest/agile/1.0/board/{board_id}/issue", params=params).json()
        items: list[Any] = data.get("issues", []) if isinstance(data, dict) else data
        return [Issue.model_validate(i) for i in items]

    def list_sprints(self, board_id: int | str, state: str | None = None) -> list[Sprint]:
        params: dict[str, Any] = {}
        if state:
            params["state"] = state
        data = self.get(f"/rest/agile/1.0/board/{board_id}/sprint", params=params or None).json()
        items: list[Any] = data if isinstance(data, list) else data.get("values", [])
        return [Sprint.model_validate(s) for s in items]

    def get_sprint_issues(self, sprint_id: int | str, limit: int = 50) -> SearchResult:
        return self.search(f'sprint="{sprint_id}"', max_results=limit)

    # ------------------------------------------------------------------
    # Dev info
    # ------------------------------------------------------------------

    def _resolve_issue_id(self, key_or_id: str) -> str:
        """Return numeric issue ID. Accepts either a key (PROJ-123) or numeric ID."""
        if key_or_id.isdigit():
            return key_or_id
        resp = self.get(f"/rest/api/2/issue/{key_or_id}", params={"fields": ""})
        return str(resp.json()["id"])

    def get_dev_info(self, key: str) -> dict[str, Any]:
        issue_id = self._resolve_issue_id(key)
        # Aggregate data from multiple app types and data types (mcp-atlassian pattern)
        merged: dict[str, Any] = {"errors": [], "detail": []}
        for app_type in ("stash", "bitbucket", "github", "gitlab"):
            for data_type in ("repository", "pullrequest", "branch"):
                try:
                    resp = self.get(
                        "/rest/dev-status/1.0/issue/detail",
                        params={"issueId": issue_id, "applicationType": app_type, "dataType": data_type},
                    ).json()
                    detail = resp.get("detail", [])
                    if detail:
                        merged["detail"].extend(detail)
                    errors = resp.get("errors", [])
                    if errors:
                        merged["errors"].extend(errors)
                except Exception:  # noqa: BLE001
                    continue
        return merged

    def get_dev_info_many(self, keys: list[str]) -> dict[str, Any]:
        # Convert any issue keys to numeric IDs (API requires numeric IDs)
        issue_ids = [self._resolve_issue_id(k) for k in keys]
        result: dict[str, Any] = self.get(
            "/rest/dev-status/1.0/issue/summary",
            params={"issueId": ",".join(issue_ids)},
        ).json()
        return result

    # ------------------------------------------------------------------
    # Links, worklogs, watchers
    # ------------------------------------------------------------------

    def list_link_types(self) -> list[LinkType]:
        data = self.get("/rest/api/2/issueLinkType").json()
        items: list[Any] = data if isinstance(data, list) else data.get("issueLinkTypes", [])
        return [LinkType.model_validate(lt) for lt in items]

    def list_worklogs(self, key: str) -> WorklogList:
        data = self.get(f"/rest/api/2/issue/{key}/worklog").json()
        return WorklogList.model_validate(data)

    def list_watchers(self, key: str) -> WatcherList:
        data = self.get(f"/rest/api/2/issue/{key}/watchers").json()
        return WatcherList.model_validate(data)

    # ==================================================================
    # WRITE methods
    # ==================================================================

    # ------------------------------------------------------------------
    # Issue CRUD
    # ------------------------------------------------------------------

    def create_issue(self, fields: dict[str, Any]) -> dict[str, Any]:
        """POST /rest/api/2/issue — create a single issue."""
        result: dict[str, Any] = self.post("/rest/api/2/issue", json={"fields": fields}).json()
        return result

    def batch_create_issues(self, issues: list[dict[str, Any]]) -> dict[str, Any]:
        """POST /rest/api/2/issue/bulk — create multiple issues."""
        payload = {"issueUpdates": [{"fields": i} for i in issues]}
        result: dict[str, Any] = self.post("/rest/api/2/issue/bulk", json=payload).json()
        return result

    def update_issue(
        self,
        key: str,
        fields: dict[str, Any] | None = None,
        update: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """PUT /rest/api/2/issue/{key} — update an issue."""
        body: dict[str, Any] = {}
        if fields:
            body["fields"] = fields
        if update:
            body["update"] = update
        resp = self.put(f"/rest/api/2/issue/{key}", json=body)
        if resp.status_code == 204:
            return None
        result: dict[str, Any] = resp.json()
        return result

    def delete_issue(self, key: str) -> None:
        """DELETE /rest/api/2/issue/{key} — delete an issue (204)."""
        self.delete(f"/rest/api/2/issue/{key}")

    def transition_issue(
        self,
        key: str,
        transition_id: str,
        fields: dict[str, Any] | None = None,
        comment: str | None = None,
    ) -> None:
        """POST /rest/api/2/issue/{key}/transitions — transition an issue (204)."""
        body: dict[str, Any] = {"transition": {"id": transition_id}}
        if fields:
            body["fields"] = fields
        if comment:
            body["update"] = {
                "comment": [{"add": {"body": comment}}],
            }
        self.post(f"/rest/api/2/issue/{key}/transitions", json=body)

    # ------------------------------------------------------------------
    # Comment / Worklog
    # ------------------------------------------------------------------

    def list_comments(self, key: str) -> list[JiraComment]:
        """GET /rest/api/2/issue/{key}/comment — list all comments."""
        data = self.get(f"/rest/api/2/issue/{key}/comment").json()
        items: list[Any] = data.get("comments", []) if isinstance(data, dict) else []
        return [JiraComment.model_validate(c) for c in items]

    def add_comment(
        self,
        key: str,
        body: str,
        visibility: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /rest/api/2/issue/{key}/comment."""
        payload: dict[str, Any] = {"body": body}
        if visibility:
            payload["visibility"] = visibility
        result: dict[str, Any] = self.post(f"/rest/api/2/issue/{key}/comment", json=payload).json()
        return result

    def edit_comment(self, key: str, comment_id: str, body: str) -> dict[str, Any]:
        """PUT /rest/api/2/issue/{key}/comment/{id}."""
        result: dict[str, Any] = self.put(
            f"/rest/api/2/issue/{key}/comment/{comment_id}",
            json={"body": body},
        ).json()
        return result

    def delete_comment(self, key: str, comment_id: str) -> None:
        """DELETE /rest/api/2/issue/{key}/comment/{id} — 204."""
        self.delete(f"/rest/api/2/issue/{key}/comment/{comment_id}")

    def add_worklog(
        self,
        key: str,
        time_spent_seconds: int,
        comment: str | None = None,
        started: str | None = None,
    ) -> dict[str, Any]:
        """POST /rest/api/2/issue/{key}/worklog."""
        payload: dict[str, Any] = {"timeSpentSeconds": time_spent_seconds}
        if comment:
            payload["comment"] = comment
        if started:
            payload["started"] = started
        result: dict[str, Any] = self.post(f"/rest/api/2/issue/{key}/worklog", json=payload).json()
        return result

    # ------------------------------------------------------------------
    # Links
    # ------------------------------------------------------------------

    def create_issue_link(
        self,
        type_name: str,
        inward_key: str,
        outward_key: str,
    ) -> dict[str, Any] | None:
        """POST /rest/api/2/issueLink."""
        payload: dict[str, Any] = {
            "type": {"name": type_name},
            "inwardIssue": {"key": inward_key},
            "outwardIssue": {"key": outward_key},
        }
        resp = self.post("/rest/api/2/issueLink", json=payload)
        if resp.status_code in (200, 201):
            result: dict[str, Any] = resp.json()
            return result
        return None

    def list_remote_issue_links(self, key: str) -> list[dict[str, Any]]:
        """GET /rest/api/2/issue/{key}/remotelink."""
        data = self.get(f"/rest/api/2/issue/{key}/remotelink").json()
        if isinstance(data, list):
            return data
        return []

    def create_remote_issue_link(
        self,
        key: str,
        url: str,
        title: str,
        relationship: str | None = None,
    ) -> dict[str, Any]:
        """POST /rest/api/2/issue/{key}/remotelink."""
        payload: dict[str, Any] = {"object": {"url": url, "title": title}}
        if relationship:
            payload["relationship"] = relationship
        result: dict[str, Any] = self.post(
            f"/rest/api/2/issue/{key}/remotelink", json=payload
        ).json()
        return result

    def remove_issue_link(self, link_id: str) -> None:
        """DELETE /rest/api/2/issueLink/{id} — 204."""
        self.delete(f"/rest/api/2/issueLink/{link_id}")

    def link_to_epic(self, key: str, epic_key: str, epic_field_id: str) -> dict[str, Any] | None:
        """PUT /rest/api/2/issue/{key} — set epic link via customfield."""
        return self.update_issue(key, fields={epic_field_id: epic_key})

    # ------------------------------------------------------------------
    # Watchers
    # ------------------------------------------------------------------

    def add_watcher(self, key: str, username: str) -> None:
        """POST /rest/api/2/issue/{key}/watchers — body is a plain JSON string (204)."""
        self.request(
            "POST",
            f"/rest/api/2/issue/{key}/watchers",
            json=username,
        )

    def remove_watcher(self, key: str, username: str) -> None:
        """DELETE /rest/api/2/issue/{key}/watchers?username={user} — 204."""
        self.request(
            "DELETE",
            f"/rest/api/2/issue/{key}/watchers",
            params={"username": username},
        )

    # ------------------------------------------------------------------
    # Sprint (Agile write)
    # ------------------------------------------------------------------

    def create_sprint(
        self,
        name: str,
        board_id: int,
        start_date: str | None = None,
        end_date: str | None = None,
        goal: str | None = None,
    ) -> dict[str, Any]:
        """POST /rest/agile/1.0/sprint."""
        payload: dict[str, Any] = {"name": name, "originBoardId": board_id}
        if start_date:
            payload["startDate"] = start_date
        if end_date:
            payload["endDate"] = end_date
        if goal:
            payload["goal"] = goal
        result: dict[str, Any] = self.post("/rest/agile/1.0/sprint", json=payload).json()
        return result

    def update_sprint(
        self,
        sprint_id: int | str,
        name: str | None = None,
        state: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        goal: str | None = None,
    ) -> dict[str, Any]:
        """PUT /rest/agile/1.0/sprint/{id}."""
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if state is not None:
            payload["state"] = state
        if start_date is not None:
            payload["startDate"] = start_date
        if end_date is not None:
            payload["endDate"] = end_date
        if goal is not None:
            payload["goal"] = goal
        result: dict[str, Any] = self.put(f"/rest/agile/1.0/sprint/{sprint_id}", json=payload).json()
        return result

    def add_issues_to_sprint(self, sprint_id: int | str, issue_keys: list[str]) -> None:
        """POST /rest/agile/1.0/sprint/{id}/issue — 204."""
        self.post(f"/rest/agile/1.0/sprint/{sprint_id}/issue", json={"issues": issue_keys})

    # ------------------------------------------------------------------
    # Version
    # ------------------------------------------------------------------

    def create_version(
        self,
        project: str,
        name: str,
        start_date: str | None = None,
        release_date: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """POST /rest/api/2/version."""
        payload: dict[str, Any] = {"project": project, "name": name}
        if start_date:
            payload["startDate"] = start_date
        if release_date:
            payload["releaseDate"] = release_date
        if description:
            payload["description"] = description
        result: dict[str, Any] = self.post("/rest/api/2/version", json=payload).json()
        return result

    def batch_create_versions(self, versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Sequential POST /rest/api/2/version for each version dict."""
        results: list[dict[str, Any]] = []
        for v in versions:
            result: dict[str, Any] = self.post("/rest/api/2/version", json=v).json()
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Attachment (write)
    # ------------------------------------------------------------------

    def upload_attachment(self, key: str, file_path: str) -> dict[str, Any]:
        """POST /rest/api/2/issue/{key}/attachments — multipart upload."""
        import contextlib
        from pathlib import Path

        from atlassian_skills.core.errors import http_error_to_atlas

        p = Path(file_path)
        url = f"{self.base_url}/rest/api/2/issue/{key}/attachments"
        headers = {**self.credential.to_header(), "X-Atlassian-Token": "nocheck"}
        with open(p, "rb") as f:
            files = {"file": (p.name, f, "application/octet-stream")}
            resp = self._client.post(url, files=files, headers=headers)
        if not resp.is_success:
            body_text: str | None = None
            with contextlib.suppress(Exception):
                body_text = resp.text
            raise http_error_to_atlas(resp.status_code, url, "POST", body_text)
        result: dict[str, Any] = resp.json()
        return result

    def delete_attachment(self, att_id: str) -> None:
        """DELETE /rest/api/2/attachment/{id} — 204."""
        self.delete(f"/rest/api/2/attachment/{att_id}")

    # ------------------------------------------------------------------
    # Attachments (read)
    # ------------------------------------------------------------------

    def get_attachment_content(self, key: str) -> list[JiraAttachment]:
        data: dict[str, Any] = self.get(f"/rest/api/2/issue/{key}", params={"fields": "attachment"}).json()
        items: list[dict[str, Any]] = data.get("fields", {}).get("attachment", data.get("attachments", []))
        return [JiraAttachment.model_validate(a) for a in items]

    # ------------------------------------------------------------------
    # Service desk
    # ------------------------------------------------------------------

    def list_service_desks(self) -> list[dict[str, Any]]:
        data: Any = self.get("/rest/servicedeskapi/servicedesk").json()
        result: list[dict[str, Any]] = data.get("values", data) if isinstance(data, dict) else data
        return result

    def get_service_desk_queues(self, sd_id: int | str) -> list[dict[str, Any]]:
        data: Any = self.get(f"/rest/servicedeskapi/servicedesk/{sd_id}/queue").json()
        result: list[dict[str, Any]] = data.get("values", data) if isinstance(data, dict) else data
        return result

    def get_queue_issues(self, sd_id: int | str, queue_id: int | str) -> list[dict[str, Any]]:
        data: Any = self.get(
            f"/rest/servicedeskapi/servicedesk/{sd_id}/queue/{queue_id}/issue"
        ).json()
        raw = data.get("issues", data.get("values", data)) if isinstance(data, dict) else data
        result: list[dict[str, Any]] = list(raw) if raw is not None else []
        return result
