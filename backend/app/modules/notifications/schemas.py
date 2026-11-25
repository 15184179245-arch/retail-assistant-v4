"""Employee-only notification API DTOs; never expose other recipients or freeform targets."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from backend.app.core.errors import ApiEnvelope


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")
    id: str
    task_id: str
    kind: str
    rule_version: str
    version: int
    created_at: datetime
    read_at: datetime | None


class MarkRead(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(ge=1)


class NotificationEnvelope(ApiEnvelope):
    data: NotificationOut | None


class NotificationListEnvelope(ApiEnvelope):
    data: list[NotificationOut] | None


class ReconcileOut(BaseModel):
    created: int
    scanned: int
    rule_version: str


class ReconcileEnvelope(ApiEnvelope):
    data: ReconcileOut | None
