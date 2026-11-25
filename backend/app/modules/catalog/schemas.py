from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.app.core.errors import ApiEnvelope


class CatalogSnapshotCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_system: str = Field(min_length=1, max_length=40)
    source_version: str = Field(min_length=1, max_length=80)
    source_updated_at: datetime | None = None
    payload: dict[str, Any] = Field(min_length=1)


class CatalogSnapshotOut(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: str
    source_version: str
    source_updated_at: datetime | None
    payload: dict[str, Any]
    payload_sha256: str
    captured_at: datetime


class CatalogProductOut(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: str
    organization_id: str
    store_id: str
    source_system: str
    external_code: str
    source_version: str
    source_updated_at: datetime | None
    active: bool
    version: int
    snapshot: CatalogSnapshotOut | None = None


class CatalogConflictOut(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: str
    product_id: str
    field_name: str
    source_value: dict[str, Any]
    local_value: dict[str, Any]
    status: str
    reason: str
    created_at: datetime


class CatalogProductEnvelope(ApiEnvelope):
    data: CatalogProductOut | None


class CatalogProductListEnvelope(ApiEnvelope):
    data: Any | None


class CatalogConflictListEnvelope(ApiEnvelope):
    data: Any | None


class CatalogCacheItem(BaseModel):
    """Whitelisted offline-find projection; never cost/raw/extension."""

    model_config = ConfigDict(extra="forbid")
    product_id: str
    external_code: str
    source_system: str
    source_version: str
    source_updated_at: datetime | None
    active: bool
    version: int
    name: str | None
    spec: str | None
    model: str | None
    unit: str | None
    category: str | None
    brand: str | None
    barcode: str | None
    barcode_flags: list[str]
    sell_price: float | None
    price_source_version: str
    price_captured_at: datetime
    snapshot_sha256: str


class CatalogCachePage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[CatalogCacheItem]
    next_cursor: str | None
    has_more: bool
    catalog_version: str
    generated_at: datetime
    changed: bool = True
    reset_required: bool = False


class CatalogCacheEnvelope(ApiEnvelope):
    data: CatalogCachePage | None
