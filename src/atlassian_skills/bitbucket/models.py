from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Project(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    key: str
    name: str
    description: str | None = None
    public: bool = False
    type: str | None = None  # NORMAL, PERSONAL


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
