"""OFC-002 strict JSON responses, no arbitrary model or source commands."""
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
from backend.app.core.errors import ApiEnvelope


class PasteIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=131072)


class EvidenceOut(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    version: int
    sha256: str
    byte_size: int
    content_type: str
    original_name: str
    source_channel: str
    created_at: datetime
    created_by: str


class ReceiptOut(EvidenceOut):
    receipt_id: str
    source_channel: str
    replayed: bool


class EvidencePage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[EvidenceOut]


class EvidenceEnvelope(ApiEnvelope):
    data: EvidenceOut | None


class ReceiptEnvelope(ApiEnvelope):
    data: ReceiptOut | None


class EvidenceListEnvelope(ApiEnvelope):
    data: EvidencePage | None
