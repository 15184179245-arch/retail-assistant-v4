from datetime import datetime

from pydantic import BaseModel, ConfigDict

from backend.app.core.errors import ApiEnvelope


class MediaAssetOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    category: str
    source: str
    original_name: str
    content_type: str
    byte_size: int
    sha256: str
    created_by: str
    created_at: datetime


class MediaAssetListPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[MediaAssetOut]


class MediaAssetEnvelope(ApiEnvelope):
    data: MediaAssetOut | None


class MediaAssetListEnvelope(ApiEnvelope):
    data: MediaAssetListPage | None
