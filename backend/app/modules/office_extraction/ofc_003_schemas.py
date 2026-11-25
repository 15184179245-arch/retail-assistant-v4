"""OFC-003 strict API DTOs: raw evidence pointers and human-only decisions."""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from backend.app.core.errors import ApiEnvelope


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    evidence_id: str = Field(min_length=1, max_length=36)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    field_id: str = Field(min_length=1, max_length=80)
    extraction_sha256: str = Field(min_length=64, max_length=64)
    expected_version: Literal[0]
    decision: Literal["confirmed", "rejected", "corrected"]
    corrected_text: str | None = Field(default=None, max_length=500)
    reason: str = Field(min_length=1, max_length=1000)


class SpanOut(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    span_id: str
    raw_text: str
    label: str | None
    location: dict
    warning: str | None


class CandidateOut(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    field_id: str
    field_key: Literal["product", "quantity", "unit", "price", "supplier"]
    span_id: str
    raw_text: str
    location: dict | None = None
    normalized_value: str | None
    confidence: str
    state: Literal["needs_review"]
    flags: list[str]


class ExtractionOut(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    evidence_id: str
    evidence_version: int
    evidence_sha256: str
    extractor_key: str
    extractor_version: str
    source_kind: str
    content_sha256: str
    spans: list[SpanOut]
    fields: list[CandidateOut]
    warnings: list[str]
    created_at: datetime
    replayed: bool
    reviews: list[dict] = Field(default_factory=list)


class ReviewOut(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    extraction_id: str
    extraction_sha256: str
    field_id: str
    decision: str
    corrected_text: str | None
    reason: str
    reviewed_by: str
    created_at: datetime
    version: int
    replayed: bool
    effect: Literal["candidate_review_only"] = "candidate_review_only"


class ExtractionEnvelope(ApiEnvelope):
    data: ExtractionOut | None


class ReviewEnvelope(ApiEnvelope):
    data: ReviewOut | None
