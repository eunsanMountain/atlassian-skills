from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Project(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    key: str
    name: str
    description: str | None = None
    public: bool = False
    type: str | None = None  # NORMAL, PERSONAL


class BitbucketUser(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str
    display_name: str = Field(alias="displayName")
    email: str | None = Field(default=None, alias="emailAddress")
    slug: str | None = None


class Repository(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    slug: str
    name: str
    project: Project
    description: str | None = None
    public: bool = False
    forkable: bool = True
    state: str | None = None  # AVAILABLE, etc.
    scm_id: str | None = Field(default=None, alias="scmId")
    status_message: str | None = Field(default=None, alias="statusMessage")


class BitbucketRef(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str  # refs/heads/feature/x
    display_id: str = Field(alias="displayId")  # feature/x
    latest_commit: str | None = Field(default=None, alias="latestCommit")
    repository: Repository | None = None


class PullRequestParticipant(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    user: BitbucketUser
    role: str | None = None  # AUTHOR, REVIEWER, PARTICIPANT
    approved: bool = False
    status: str | None = None  # UNAPPROVED, APPROVED, NEEDS_WORK


class PullRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    title: str
    description: str | None = None
    state: str  # OPEN, MERGED, DECLINED
    version: int | None = None
    created_date: int | None = Field(default=None, alias="createdDate")  # epoch ms
    updated_date: int | None = Field(default=None, alias="updatedDate")
    author: PullRequestParticipant | None = None
    reviewers: list[PullRequestParticipant] = Field(default_factory=list)
    participants: list[PullRequestParticipant] = Field(default_factory=list)
    from_ref: BitbucketRef | None = Field(default=None, alias="fromRef")
    to_ref: BitbucketRef | None = Field(default=None, alias="toRef")
    links: dict[str, Any] = Field(default_factory=dict)


class CommentAnchor(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    path: str | None = None
    line: int | None = None
    line_type: str | None = Field(default=None, alias="lineType")  # ADDED, REMOVED, CONTEXT
    file_type: str | None = Field(default=None, alias="fileType")  # FROM, TO
    src_path: str | None = Field(default=None, alias="srcPath")


class PullRequestComment(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    text: str | None = None
    author: BitbucketUser | None = None
    created_date: int | None = Field(default=None, alias="createdDate")
    updated_date: int | None = Field(default=None, alias="updatedDate")
    severity: str | None = None  # NORMAL, BLOCKER
    state: str | None = None  # OPEN, RESOLVED
    anchor: CommentAnchor | None = None
    comments: list[PullRequestComment] = Field(default_factory=list)  # threaded replies
    version: int | None = None  # for optimistic locking


# Self-referential model needs rebuild
PullRequestComment.model_rebuild()


class PullRequestActivity(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    action: str  # COMMENTED, APPROVED, MERGED, DECLINED, RESCOPED, etc.
    created_date: int | None = Field(default=None, alias="createdDate")
    user: BitbucketUser | None = None
    comment: PullRequestComment | None = None


class Branch(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str  # refs/heads/main
    display_id: str = Field(alias="displayId")  # main
    latest_changeset: str | None = Field(default=None, alias="latestChangeset")
    latest_commit: str | None = Field(default=None, alias="latestCommit")
    is_default: bool = Field(default=False, alias="isDefault")


class Commit(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str  # full SHA
    display_id: str = Field(alias="displayId")  # short SHA
    message: str | None = None
    author_timestamp: int | None = Field(default=None, alias="authorTimestamp")
    author: BitbucketUser | None = None
    committer: BitbucketUser | None = None


# Phase 3 models


class Task(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    text: str
    state: str  # OPEN, RESOLVED
    author: BitbucketUser | None = None
    created_date: int | None = Field(default=None, alias="createdDate")


class BuildStatus(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    state: str  # SUCCESSFUL, FAILED, INPROGRESS
    key: str
    name: str | None = None
    url: str | None = None
    description: str | None = None
    date_added: int | None = Field(default=None, alias="dateAdded")


class DiffStatPath(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    components: list[str] = Field(default_factory=list)
    to_string: str = Field(alias="toString")


class DiffStat(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    path: DiffStatPath
    type: str | None = None  # ADD, MODIFY, DELETE, RENAME, COPY
    src_path: DiffStatPath | None = Field(default=None, alias="srcPath")
