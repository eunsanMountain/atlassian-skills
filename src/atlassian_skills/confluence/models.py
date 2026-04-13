from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from atlassian_skills.jira.models import User


class Space(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str | int | None = None  # on-prem Confluence returns int
    key: str
    name: str
    type: str | None = None


class ContentBody(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    value: str = ""
    format: str = "storage"


class PageVersion(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    number: int = 1
    when: str | None = None
    by: User | None = None
    message: str | None = None


class PageLinks(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    webui: str | None = None
    self_link: str | None = Field(default=None, alias="self")
    download: str | None = None
    next_link: str | None = Field(default=None, alias="next")


class Attachment(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    title: str
    media_type: str | None = Field(default=None, alias="mediaType")
    file_size: int | None = Field(default=None, alias="fileSize")
    links: PageLinks | None = Field(default=None, alias="_links")


class Page(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    title: str
    type: str = "page"
    status: str | None = None
    space: Space | None = None
    version: PageVersion | int | None = None
    body_storage: str | None = None
    body_view: str | None = None
    ancestors: list[Page] = Field(default_factory=list)
    children: list[Page] = Field(default_factory=list)
    links: PageLinks | None = Field(default=None, alias="_links")

    # Support both preprocessed fixture format and raw API format
    url: str | None = None
    content: ContentBody | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    created: str | None = None
    updated: str | None = None
    emoji: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _extract_body_storage(cls, data: Any) -> Any:
        """Extract body_storage from nested body.storage.value if present."""
        if isinstance(data, dict) and not data.get("body_storage"):
            body = data.get("body")
            if isinstance(body, dict):
                storage = body.get("storage")
                if isinstance(storage, dict):
                    data["body_storage"] = storage.get("value", "")
        return data


# Required for self-referential model
Page.model_rebuild()


class ConfluenceSearchResult(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    results: list[Page] = Field(default_factory=list)
    start: int = 0
    limit: int = 25
    total: int = 0
    links: PageLinks | None = Field(default=None, alias="_links")


class Comment(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    title: str | None = None
    body_view: str | None = None
    version: PageVersion | None = None
    ancestors: list[Comment] = Field(default_factory=list)


Comment.model_rebuild()


class Label(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str | None = None
    name: str
    prefix: str = "global"


class SpaceTreeNode(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    title: str
    type: str = "page"
    parent_id: str | None = Field(default=None, alias="parent_id")
    position: int | str | None = None
    depth: int = 0
    ancestors: list[SpaceTreeNode] = Field(default_factory=list)
    children: list[SpaceTreeNode] = Field(default_factory=list)


SpaceTreeNode.model_rebuild()


class SpaceTreeResult(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    space_key: str
    total_pages: int = 0
    has_more: bool = False
    pages: list[SpaceTreeNode] = Field(default_factory=list)
    next_start: int | None = None
    hint: str | None = None


class PageHistory(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    title: str
    version: PageVersion | int | None = None
    previous_versions: list[PageVersion] = Field(default_factory=list)
