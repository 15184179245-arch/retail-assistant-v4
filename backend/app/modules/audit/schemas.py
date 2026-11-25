from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, field_validator

from backend.app.core.errors import ApiEnvelope


class AuditEventItem(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: str
    organization_id: str
    store_id: str
    actor_id: str
    action: str
    target_type: str
    target_id: str
    occurred_at: datetime
    created_at: datetime

    @field_validator("occurred_at", "created_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        # SQLite drops timezone metadata; every audit timestamp is stored as UTC.
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class AuditEventPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[AuditEventItem]
    next_cursor: str | None


class AuditEventsEnvelope(ApiEnvelope):
    data: AuditEventPage | None
