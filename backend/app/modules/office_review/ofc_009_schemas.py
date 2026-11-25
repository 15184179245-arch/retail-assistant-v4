"""Pydantic schemas and six-field envelopes for OFC-009 office review workbench."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar
from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReviewEnvelope(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid", strict=True)

    success: bool
    data: T | None = None
    code: str = "OK"
    error: str | None = None
    user_message: str = "ok"
    hint: str = "success"


class ReviewItemCreate(StrictSchema):
    target_kind: Literal["workflow", "exception", "document", "workpaper", "settlement", "catalog"]
    target_id: str = Field(min_length=1, max_length=36)
    target_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1000)
    risk_level: Literal["low", "medium", "high", "critical"]
    diff_payload: dict[str, Any]


class ReviewItemClaim(StrictSchema):
    item_id: str = Field(min_length=1, max_length=36)
    lock_duration_seconds: int = Field(default=300, ge=30, le=1800)


class ReviewItemDecide(StrictSchema):
    item_id: str = Field(min_length=1, max_length=36)
    expected_target_version: int = Field(ge=1)
    lock_token: str | None = Field(default=None, max_length=64)
    decision: Literal["approved", "rejected", "modified", "escalated"]
    reason: str = Field(min_length=1, max_length=1000)
    modifications: dict[str, Any] | None = None


class BatchReviewDecide(StrictSchema):
    items: list[ReviewItemDecide] = Field(min_length=1, max_length=50)


class ReviewItemOut(StrictSchema):
    id: str
    organization_id: str
    store_id: str
    target_kind: str
    target_id: str
    target_version: int
    title: str
    summary: str
    risk_level: str
    diff_payload: dict[str, Any]
    state: str
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    lock_token: str | None = None
    lock_expires_at: datetime | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_decision: str | None = None
    review_notes: str | None = None
    modifications: dict[str, Any] | None = None
    receipt_digest: str | None = None
    created_at: datetime
    updated_at: datetime


class BatchItemResult(StrictSchema):
    item_id: str
    success: bool
    decision: str
    risk_level: str
    error_code: str | None = None
    message: str


class BatchReviewOut(StrictSchema):
    total: int
    succeeded: int
    failed: int
    results: list[BatchItemResult]
