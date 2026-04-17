from __future__ import annotations

import difflib
import mimetypes
import os
from pathlib import Path
from typing import Any

from atlassian_skills.confluence.models import (
    Attachment,
    Comment,
    ConfluenceSearchResult,
    Label,
    Page,
    SpaceTreeNode,
    SpaceTreeResult,
)
from atlassian_skills.core.auth import Credential
from atlassian_skills.core.client import BaseClient
from atlassian_skills.jira.models import User


def _safe_filename(title: str, fallback_id: str) -> str:
    """Sanitize attachment filename to prevent path traversal."""
    safe = os.path.basename(title).lstrip(".")
    if not safe:
        safe = f"attachment_{fallback_id}"
    return safe


class ConfluenceClient(BaseClient):
    def __init__(self, base_url: str, credential: Credential, timeout: float = 30.0, verify: str | bool = True) -> None:
        super().__init__(base_url, credential, timeout, verify=verify)

    # ------------------------------------------------------------------
    # Page read
    # ------------------------------------------------------------------

    def get_page_raw_text(
        self,
        page_id: str,
        expand: str = "body.storage,version,space,history",
    ) -> str:
        """Return verbatim response text (byte-preserving raw contract)."""
        return self.get(
            f"/rest/api/content/{page_id}",
            params={"expand": expand},
        ).text

    def get_page_raw(
        self,
        page_id: str,
        expand: str = "body.storage,version,space,history",
    ) -> dict[str, Any]:
        """Return the verbatim server JSON response (byte-preserving raw contract)."""
        result: dict[str, Any] = self.get(
            f"/rest/api/content/{page_id}",
            params={"expand": expand},
        ).json()
        return result

    def get_page(
        self,
        page_id: str,
        expand: str = "body.storage,version,space,history",
        include_body: bool = True,
    ) -> Page:
        # Task 2: Expand minimization — omit body.storage when not needed.
        if not include_body:
            parts = [p.strip() for p in expand.split(",") if not p.strip().startswith("body")]
            expand = ",".join(parts) if parts else "version,space,history"
        data = self.get(
            f"/rest/api/content/{page_id}",
            params={"expand": expand},
        ).json()
        return Page.model_validate(data)

    def get_page_history(
        self,
        page_id: str,
        version: int,
    ) -> Page:
        """Get a specific historical version of a page."""
        data = self.get(
            f"/rest/api/content/{page_id}",
            params={
                "status": "historical",
                "version": version,
                "expand": "body.storage,version,space",
            },
        ).json()
        return Page.model_validate(data)

    def get_page_diff(
        self,
        page_id: str,
        from_ver: int,
        to_ver: int,
    ) -> str:
        """Fetch two historical versions and return a unified diff."""
        old_body = self._get_version_body(page_id, from_ver)
        new_body = self._get_version_body(page_id, to_ver)

        old_lines = old_body.splitlines(keepends=True)
        new_lines = new_body.splitlines(keepends=True)

        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"v{from_ver}",
            tofile=f"v{to_ver}",
        )
        return "".join(diff)

    def _get_version_body(self, page_id: str, version: int) -> str:
        """Fetch a historical version and extract its body text."""
        data: dict[str, Any] = self.get(
            f"/rest/api/content/{page_id}",
            params={
                "status": "historical",
                "version": version,
                "expand": "body.storage,version,space",
            },
        ).json()
        # Extract body from nested API structure: body.storage.value
        body = data.get("body", {}).get("storage", {}).get("value", "")
        if not body:
            # Fallback: preprocessed format with content.value
            body = data.get("content", {}).get("value", "") if isinstance(data.get("content"), dict) else ""
        return str(body)

    _IMAGE_EXTENSIONS: frozenset[str] = frozenset(
        {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico", ".tiff"}
    )

    def get_page_images(self, page_id: str) -> list[Attachment]:
        """List image attachments on a page.

        Extension fallback applies only when media_type is missing or ambiguous,
        not when the server explicitly reports a non-image MIME type.
        """
        attachments = self.list_attachments(page_id)
        result: list[Attachment] = []
        for a in attachments:
            mime = (a.media_type or "").lower()
            if mime.startswith("image/"):
                result.append(a)
                continue
            if not mime or mime in ("application/octet-stream", "binary/octet-stream"):
                if any(a.title.lower().endswith(ext) for ext in self._IMAGE_EXTENSIONS):
                    result.append(a)
        return result

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        cql: str,
        limit: int = 25,
    ) -> ConfluenceSearchResult:
        # Peek at first page to capture server-side totalSize before delegating to paginator
        first_resp = self.get(
            "/rest/api/search",
            params={"cql": cql, "limit": limit},
        ).json()
        total_size = first_resp.get("totalSize", first_resp.get("size", 0))
        items: list[Any] = list(first_resp.get("results", []))
        # Follow _links.next if present and we haven't hit the limit
        next_url = first_resp.get("_links", {}).get("next")
        while next_url and len(items) < limit:
            next_page = self.get(next_url).json()
            items.extend(next_page.get("results", []))
            next_url = next_page.get("_links", {}).get("next")
        items = items[:limit]
        # /rest/api/search on Server/DC wraps page data inside a "content"
        # key that has {id, title, …}.  Unwrap only when "content" looks like
        # a page object (has "id"), not when it is body content (has "value").
        pages: list[Page] = []
        for i in items:
            if isinstance(i, dict):
                inner = i.get("content")
                if isinstance(inner, dict) and "id" in inner:
                    i = inner
            pages.append(Page.model_validate(i))
        return ConfluenceSearchResult(
            results=pages,
            total=total_size,
            limit=limit,
        )

    # ------------------------------------------------------------------
    # Children
    # ------------------------------------------------------------------

    def get_children(
        self,
        page_id: str,
        limit: int = 25,
    ) -> list[Page]:
        items = self.get_paginated_links(
            f"/rest/api/content/{page_id}/child/page",
            params={"limit": limit},
            items_key="results",
            limit=limit,
        )
        return [Page.model_validate(i) for i in items]

    # ------------------------------------------------------------------
    # Space tree
    # ------------------------------------------------------------------

    def get_space_tree(
        self,
        space_key: str,
        limit: int = 200,
    ) -> SpaceTreeResult:
        # /rest/api/space/{key}/content nests pages under "page.results",
        # not top-level "results", so we can't use get_paginated_links directly.
        resp = self.get(
            f"/rest/api/space/{space_key}/content",
            params={"type": "page", "expand": "ancestors", "limit": limit},
        ).json()
        page_section = resp.get("page", {})
        raw_items: list[dict[str, Any]] = page_section.get("results", [])
        nodes = [SpaceTreeNode.model_validate(i) for i in raw_items]
        # Derive depth from ancestors list
        for node in nodes:
            if node.ancestors:
                node.depth = len(node.ancestors)
                node.parent_id = node.ancestors[-1].id
        return SpaceTreeResult(
            space_key=space_key,
            total_pages=len(nodes),
            has_more=bool(page_section.get("_links", {}).get("next")),
            pages=sorted(nodes, key=lambda n: (n.depth, n.title)),
        )

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

    def list_comments(
        self,
        page_id: str,
    ) -> list[Comment]:
        items = self.get_paginated_links(
            f"/rest/api/content/{page_id}/child/comment",
            params={"expand": "body.view.value,version", "depth": "all"},
            items_key="results",
        )
        return [Comment.model_validate(i) for i in items]

    # ------------------------------------------------------------------
    # Labels
    # ------------------------------------------------------------------

    def list_labels(self, page_id: str) -> list[Label]:
        data = self.get(f"/rest/api/content/{page_id}/label").json()
        items: list[Any] = data if isinstance(data, list) else data.get("results", [])
        return [Label.model_validate(i) for i in items]

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    def list_attachments(
        self,
        page_id: str,
        limit: int = 50,
    ) -> list[Attachment]:
        items = self.get_paginated_links(
            f"/rest/api/content/{page_id}/child/attachment",
            params={"limit": limit, "expand": "version,extensions.mediaType,extensions.fileSize"},
            items_key="results",
            limit=limit,
        )
        # Attachment.model_validator extracts extensions.mediaType/fileSize automatically
        return [Attachment.model_validate(i) for i in items]

    def download_attachment(
        self,
        att_id: str,
        output_path: str | Path,
        *,
        download_link: str | None = None,
    ) -> Path:
        """Download a single attachment.

        If *download_link* (the ``_links.download`` path from the API) is
        provided it is used directly.  Otherwise the attachment metadata is
        fetched first to obtain the correct download path.  The previous
        ``/rest/api/content/{id}/download`` endpoint does not exist on
        Server/DC — see GitHub issue #1.
        """
        if not download_link:
            meta = self.get(f"/rest/api/content/{att_id}", params={"expand": ""}).json()
            download_link = meta.get("_links", {}).get("download")
            if not download_link:
                from atlassian_skills.core.errors import NotFoundError

                raise NotFoundError(f"No download link found for attachment {att_id}")
        resp = self.get(download_link)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(resp.content)
        return out

    def download_all_attachments(self, page_id: str, output_dir: str | Path) -> list[Path]:
        """Download all attachments for a page to output_dir."""
        attachments = self.list_attachments(page_id)
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for att in attachments:
            safe_name = _safe_filename(att.title, att.id)
            dest = out_dir / safe_name
            # Verify no path traversal
            if not dest.resolve().is_relative_to(out_dir.resolve()):
                raise ValueError(f"Path traversal detected in attachment title: {att.title!r}")
            self.download_attachment(att.id, dest, download_link=att.links.download if att.links else None)
            paths.append(dest)
        return paths

    # ------------------------------------------------------------------
    # User
    # ------------------------------------------------------------------

    def get_current_user(self) -> User:
        """GET /rest/api/user/current — current authenticated user."""
        data = self.get("/rest/api/user/current").json()
        return User.model_validate(data)

    # ------------------------------------------------------------------
    # User search (group member + fuzzy match)
    # ------------------------------------------------------------------

    def search_users(
        self,
        query: str,
        group_name: str = "confluence-users",
        limit: int = 200,
    ) -> list[User]:
        from urllib.parse import quote

        encoded_group = quote(group_name, safe="")
        items = self.get_paginated_links(
            f"/rest/api/group/{encoded_group}/member",
            params={"limit": limit},
            items_key="results",
            limit=limit,
        )
        all_users = [User.model_validate(i) for i in items]
        if not query:
            return all_users
        q = query.lower()
        return [
            u
            for u in all_users
            if q in (u.display_name or "").lower() or q in (u.name or "").lower() or q in (u.email or "").lower()
        ]

    # ==================================================================
    # Write operations
    # ==================================================================

    # ------------------------------------------------------------------
    # Page CRUD
    # ------------------------------------------------------------------

    def create_page(
        self,
        space_key: str,
        title: str,
        body: str,
        ancestor_id: str | None = None,
        body_format: str = "storage",
    ) -> dict[str, Any]:
        """Create a new Confluence page."""
        payload: dict[str, Any] = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "body": {body_format: {"value": body, "representation": body_format}},
        }
        if ancestor_id:
            payload["ancestors"] = [{"id": ancestor_id}]
        return self.post("/rest/api/content", json=payload).json()  # type: ignore[no-any-return]

    def update_page(
        self,
        page_id: str,
        title: str,
        body: str,
        version_number: int,
        body_format: str = "storage",
    ) -> dict[str, Any]:
        """Update an existing Confluence page (optimistic concurrency via version)."""
        payload: dict[str, Any] = {
            "type": "page",
            "title": title,
            "body": {body_format: {"value": body, "representation": body_format}},
            "version": {"number": version_number},
        }
        return self.put(f"/rest/api/content/{page_id}", json=payload).json()  # type: ignore[no-any-return]

    def delete_page(self, page_id: str) -> None:
        """Delete a Confluence page."""
        self.delete(f"/rest/api/content/{page_id}")

    def move_page(
        self,
        page_id: str,
        position: str,
        target_id: str,
    ) -> dict[str, Any]:
        """Move a page relative to a target (append|above|below)."""
        return self.post(  # type: ignore[no-any-return]
            f"/rest/api/content/{page_id}/move/{position}/target/{target_id}",
        ).json()

    # ------------------------------------------------------------------
    # Comment write
    # ------------------------------------------------------------------

    def add_comment(
        self,
        page_id: str,
        body: str,
        body_format: str = "storage",
    ) -> dict[str, Any]:
        """Add a comment to a page.

        Uses POST /rest/api/content with a container field — the /child/comment
        sub-resource only accepts GET on Confluence Server/DC (POST returns 405).
        """
        payload: dict[str, Any] = {
            "type": "comment",
            "container": {"id": page_id, "type": "page"},
            "body": {body_format: {"value": body, "representation": body_format}},
        }
        return self.post(  # type: ignore[no-any-return]
            "/rest/api/content",
            json=payload,
        ).json()

    def reply_to_comment(
        self,
        comment_id: str,
        body: str,
        body_format: str = "storage",
    ) -> dict[str, Any]:
        """Reply to an existing comment.

        Fetches the parent comment to resolve its container page, then creates
        a new comment under that page with the parent as an ancestor.
        """
        parent = self.get(f"/rest/api/content/{comment_id}?expand=container").json()
        container_id = parent.get("container", {}).get("id", comment_id)

        payload: dict[str, Any] = {
            "type": "comment",
            "container": {"id": container_id, "type": "page"},
            "ancestors": [{"id": comment_id}],
            "body": {body_format: {"value": body, "representation": body_format}},
        }
        return self.post(  # type: ignore[no-any-return]
            "/rest/api/content",
            json=payload,
        ).json()

    # ------------------------------------------------------------------
    # Label write
    # ------------------------------------------------------------------

    def add_label(
        self,
        page_id: str,
        labels: list[str],
    ) -> dict[str, Any]:
        """Add labels to a page."""
        payload = [{"name": name, "prefix": "global"} for name in labels]
        return self.post(  # type: ignore[no-any-return]
            f"/rest/api/content/{page_id}/label",
            json=payload,
        ).json()

    # ------------------------------------------------------------------
    # Attachment write
    # ------------------------------------------------------------------

    def upload_attachment(
        self,
        page_id: str,
        file_path: str | Path,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """Upload a single attachment to a page."""
        return self._upload_attachment_raw(page_id, Path(file_path), comment)

    def _upload_attachment_raw(
        self,
        page_id: str,
        path: Path,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """Low-level attachment upload using httpx multipart."""
        url = f"{self.base_url}/rest/api/content/{page_id}/child/attachment"
        mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        headers = {
            **self.credential.to_header(),
            "X-Atlassian-Token": "nocheck",
        }
        with open(path, "rb") as f:
            files = {"file": (path.name, f, mime_type)}
            data: dict[str, str] | None = {"comment": comment} if comment else None
            resp = self._client.post(url, files=files, data=data, headers=headers)
        if not resp.is_success:
            from atlassian_skills.core.errors import http_error_to_atlas

            raise http_error_to_atlas(resp.status_code, url, "POST", resp.text)
        return resp.json()  # type: ignore[no-any-return]

    def upload_attachments_batch(
        self,
        page_id: str,
        file_paths: list[str | Path],
        if_exists: str = "skip",
    ) -> list[dict[str, Any]]:
        """Upload multiple attachments sequentially.

        if_exists: "skip" (check existing by title), "replace", "version"
        """
        existing_titles: set[str] | None = None
        if if_exists == "skip":
            existing = self.list_attachments(page_id)
            existing_titles = {a.title for a in existing}

        results: list[dict[str, Any]] = []
        for fp in file_paths:
            path = Path(fp)
            if if_exists == "skip" and existing_titles and path.name in existing_titles:
                results.append({"title": path.name, "skipped": True})
                continue
            result = self._upload_attachment_raw(page_id, path)
            results.append(result)
        return results

    def delete_attachment(self, att_id: str) -> None:
        """Delete an attachment by its content ID."""
        self.delete(f"/rest/api/content/{att_id}")
