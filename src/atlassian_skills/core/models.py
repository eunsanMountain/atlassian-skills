from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class WriteResult(BaseModel):
    """Shared model for write operation results (compact output only)."""

    model_config = ConfigDict(extra="ignore")

    action: str  # created, updated, transitioned, deleted, commented
    key: str
    summary: str | None = None
    id: str | None = None
