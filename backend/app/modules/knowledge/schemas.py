from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.core.errors import ApiEnvelope


class KnowledgeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)
    source_type: Literal["manual", "file", "link", "import"]
    source_ref: str | None = Field(default=None, max_length=500)
    scope_type: Literal["store", "product", "category", "global"] = "store"
    scope_ref: str | None = Field(default=None, max_length=200)
    expires_at: datetime | None = None


class KnowledgeRevisionCreate(KnowledgeCreate):
    """A correction keeps the entry and appends one revision."""
    expected_version: int = Field(ge=1, strict=True)


class KnowledgeReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)
    revision_id: str = Field(min_length=1, max_length=36)
    decision: Literal["approve", "reject", "verify_source"]
    reason: str | None = Field(default=None, max_length=500)


class KnowledgeWithdraw(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)
    reason: str = Field(min_length=1, max_length=500)


class KnowledgeRevisionOut(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: str
    version: int
    title: str
    body: str
    source_type: str
    source_ref: str | None
    submitted_by: str
    submitted_at: datetime


class KnowledgeEntryOut(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: str
    organization_id: str
    store_id: str
    scope_type: str
    scope_ref: str | None
    status: str
    version: int
    submitted_by: str
    submitted_at: datetime
    approved_by: str | None
    approved_at: datetime | None
    expires_at: datetime | None
    revision: KnowledgeRevisionOut | None = None


class KnowledgeDetailOut(KnowledgeEntryOut):
    history: list[KnowledgeRevisionOut] = Field(default_factory=list)


class KnowledgeEnvelope(ApiEnvelope):
    data: KnowledgeEntryOut | None


class KnowledgeDetailEnvelope(ApiEnvelope):
    data: KnowledgeDetailOut | None


class KnowledgeListEnvelope(ApiEnvelope):
    data: Any | None
