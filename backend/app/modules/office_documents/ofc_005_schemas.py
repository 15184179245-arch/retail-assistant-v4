"""OFC-005 strict employee document contracts; amounts are decimal strings."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from backend.app.core.errors import ApiEnvelope


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Line(Strict):
    line_ref: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=150)
    unit: str = Field(min_length=1, max_length=24)
    quantity: str = Field(min_length=1, max_length=24)
    unit_price: str = Field(min_length=1, max_length=24)


class DocumentCreate(Strict):
    document_key: str = Field(min_length=1, max_length=120)
    expected_version: int = Field(ge=0)
    document_type: Literal["quotation","reconciliation","receiving_list"]
    evidence_id: str = Field(min_length=1, max_length=36)
    evidence_sha256: str = Field(min_length=64, max_length=64)
    currency: Literal["CNY","USD"]
    title: str = Field(min_length=1, max_length=120)
    lines: list[Line] = Field(min_length=1, max_length=60)


class DocumentDecision(Strict):
    expected_version: int = Field(ge=1)
    content_sha256: str = Field(min_length=64, max_length=64)
    decision: Literal["approved","rejected"]
    reason: str = Field(min_length=1, max_length=1000)


class DocumentExport(Strict):
    expected_version: int = Field(ge=1)
    content_sha256: str = Field(min_length=64, max_length=64)
    file_format: Literal["xlsx","pdf"]


class DocumentEnvelope(ApiEnvelope):
    data: dict | None = None
