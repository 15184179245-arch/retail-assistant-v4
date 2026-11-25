from datetime import datetime

from pydantic import BaseModel, ConfigDict

from backend.app.core.errors import ApiEnvelope


class LocationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    kind: str
    parent_id: str | None = None


class LocationOut(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: str
    parent_id: str | None
    kind: str
    name: str
    active: bool
    created_at: datetime
    updated_at: datetime


class AssertionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str
    kind: str
    idempotency_key: str


class AssertionOut(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: str
    product_id: str
    location_id: str
    kind: str
    asserted_by: str
    asserted_at: datetime


class LocationEnvelope(ApiEnvelope):
    data: LocationOut | None


class AssertionEnvelope(ApiEnvelope):
    data: AssertionOut | None


class AssertionListEnvelope(ApiEnvelope):
    data: dict | None
