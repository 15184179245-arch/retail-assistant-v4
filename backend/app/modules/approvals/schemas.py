from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.core.errors import ApiEnvelope


class ApprovalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_type: str = Field(min_length=1, max_length=100)
    subject_id: str = Field(min_length=1, max_length=200)
    action_key: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=4000)
    expires_at: datetime


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=1, max_length=4000)


class ApprovalCancellation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=4000)


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: str
    organization_id: str
    store_id: str
    requester_id: str
    subject_type: str
    subject_id: str
    action_key: str
    reason: str
    state: Literal["requested", "approved", "rejected", "cancelled", "expired"]
    requested_at: datetime
    expires_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    decision_reason: str | None
    cancelled_by: str | None
    cancelled_at: datetime | None
    cancellation_reason: str | None
    expired_by: str | None
    expired_at: datetime | None

    @field_validator(
        "requested_at", "expires_at", "decided_at", "cancelled_at", "expired_at",
        mode="after",
    )
    @classmethod
    def normalize_times_to_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            # SQLite drops timezone metadata; stored application values are UTC.
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class ApprovalEnvelope(ApiEnvelope):
    data: ApprovalResponse | None


class ApprovalPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ApprovalResponse]
    next_cursor: str | None


class ApprovalListEnvelope(ApiEnvelope):
    data: ApprovalPage | None
