from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class User(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    display_name: str = Field(alias="displayName")
    name: str | None = None
    email: str | None = Field(default=None, alias="emailAddress")
    avatar_url: str | None = Field(default=None, alias="avatarUrl")
    key: str | None = None


class Status(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str
    category: str | None = None
    color: str | None = None


class IssueType(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str
    description: str | None = None
    icon_url: str | None = None
    subtask: bool = False


class Priority(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str
    icon_url: str | None = None


class Project(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str | None = None
    key: str
    name: str
    description: str | None = None
    project_type_key: str | None = None
    lead: User | None = None


class ProjectVersion(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    name: str
    description: str | None = None
    released: bool = False
    archived: bool = False
    release_date: str | None = None


class ProjectComponent(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    name: str
    description: str | None = None
    lead: User | None = None


class Sprint(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    name: str
    state: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    complete_date: str | None = None
    board_id: int | None = None


class LinkType(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    name: str
    inward: str | None = None
    outward: str | None = None


class Issue(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    key: str
    summary: str = ""
    description: str | None = None
    status: Status | None = None
    issue_type: IssueType | None = Field(default=None, alias="issuetype")
    priority: Priority | None = None
    assignee: User | None = None
    reporter: User | None = None
    project: Project | None = None
    labels: list[str] = Field(default_factory=list)
    components: list[ProjectComponent] = Field(default_factory=list)
    fix_versions: list[ProjectVersion] = Field(default_factory=list)
    created: str | None = None
    updated: str | None = None
    resolution_date: str | None = None
    due_date: str | None = None
    parent: Issue | None = None
    custom_fields: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _flatten_fields(cls, data: Any) -> Any:
        """Flatten Jira REST API v2 'fields' envelope into top level."""
        if isinstance(data, dict) and "fields" in data:
            fields = data.get("fields", {})
            if isinstance(fields, dict):
                flat = {k: v for k, v in data.items() if k != "fields"}
                flat.update(fields)
                flat["custom_fields"] = {
                    key: value for key, value in fields.items() if key.startswith("customfield_")
                }
                return flat
        return data


# Required for self-referential model
Issue.model_rebuild()


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    total: int
    start_at: int = Field(default=0, alias="startAt")
    max_results: int = Field(alias="maxResults")
    issues: list[Issue] = Field(default_factory=list)


class Transition(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    name: str
    to_status: Status | None = None


class FieldSchema(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    type: str | None = None
    custom: str | None = None
    custom_id: int | None = Field(default=None, alias="customId")


class JiraField(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    name: str
    custom: bool = False
    orderable: bool = False
    navigable: bool = False
    searchable: bool = False
    clause_names: list[str] = Field(default_factory=list, alias="clauseNames")
    field_schema: FieldSchema | None = Field(default=None, alias="schema")


class Board(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    name: str
    type: str


class Worklog(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str | None = None
    author: User | None = None
    comment: str | None = None
    created: str | None = None
    updated: str | None = None
    started: str | None = None
    time_spent: str | None = None
    time_spent_seconds: int | None = None


class WorklogList(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    worklogs: list[Worklog] = Field(default_factory=list)
    total: int | None = None
    start_at: int | None = None
    max_results: int | None = None


class WatcherList(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    issue_key: str | None = None
    watcher_count: int = 0
    is_watching: bool = False
    watchers: list[User] = Field(default_factory=list)
