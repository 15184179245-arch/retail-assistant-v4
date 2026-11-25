"""STK-003 DTOs. Every observation is partial: no full-store inventory totals."""
from datetime import datetime
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from backend.app.core.errors import ApiEnvelope


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    coverage_kind: Literal["partial_area", "partial_shelf", "unknown"]
    coverage_location_id: str | None = Field(default=None, max_length=36)
    coverage_note: str = Field(min_length=1, max_length=500)


class ObservationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_session_version: int = Field(gt=0, strict=True)
    kind: Literal["observed", "correction"] = "observed"
    supersedes_id: str | None = Field(default=None, min_length=36, max_length=36)
    product_id: str | None = Field(default=None, min_length=36, max_length=36)
    unknown_product_label: str | None = Field(default=None, min_length=1, max_length=200)
    location_id: str | None = Field(default=None, min_length=36, max_length=36)
    unknown_location_label: str | None = Field(default=None, min_length=1, max_length=200)
    quantity: Decimal = Field(ge=0, le=1_000_000, decimal_places=3)
    unit: Literal["piece", "box", "kg", "meter"]
    media_id: str | None = Field(default=None, min_length=36, max_length=36)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone_required")
        return value

    @model_validator(mode="after")
    def identity_and_correction(self):
        if (self.product_id is None) == (self.unknown_product_label is None):
            raise ValueError("exactly_one_product_identity")
        if (self.location_id is None) == (self.unknown_location_label is None):
            raise ValueError("exactly_one_location_identity")
        if (self.kind == "observed") != (self.supersedes_id is None):
            raise ValueError("correction_reference_required")
        return self


class StocktakeSessionOut(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: str
    coverage_kind: str
    coverage_location_id: str | None
    coverage_note: str
    status: str
    version: int
    opened_by: str
    created_at: datetime


class ObservationOut(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: str
    event_seq: int
    session_id: str
    kind: str
    supersedes_id: str | None
    product_id: str | None
    unknown_product_label: str | None
    location_id: str | None
    unknown_location_label: str | None
    quantity: str
    unit: str
    media_id: str | None
    observed_by: str
    observed_at: datetime
    recorded_at: datetime


class SessionEnvelope(ApiEnvelope):
    data: StocktakeSessionOut | None


class SessionsEnvelope(ApiEnvelope):
    data: list[StocktakeSessionOut] | None


class ObservationEnvelope(ApiEnvelope):
    data: ObservationOut | None


class ObservationsEnvelope(ApiEnvelope):
    data: list[ObservationOut] | None


# STK-004: only existing stocktake.read/write grants, no untrusted actor,
# reviewer, book balance, business-stock mutation or implicit full coverage.
class ConversionVerify(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_session_version: int = Field(gt=0)
    product_id: str = Field(min_length=1, max_length=36)
    source_unit: Literal["box"]
    target_unit: Literal["piece"]
    factor: str = Field(pattern=r"^[1-9][0-9]{0,5}$")
    evidence_media_id: str = Field(min_length=1, max_length=36)
    effective_from: datetime
    expires_at: datetime

    @field_validator("effective_from", "expires_at")
    @classmethod
    def aware_conversion(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone_required")
        return value


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_session_version: int = Field(gt=0)
    decision: Literal["accepted_partial", "needs_recount"]
    reason: str = Field(min_length=3, max_length=2000)
    observation_ids: list[str] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_sources(self):
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError("duplicate_observation_ids")
        return self


class BlindSessionView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    session_version: int
    coverage_kind: str
    coverage_location_id: str | None
    coverage_note: str
    opened_at: datetime
    blind: bool
    book_quantity: None
    book_difference: None
    inventory_accuracy: None
    full_store_coverage: bool
    message: str


class ReviewPreviewView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    session_version: int
    coverage_kind: str
    coverage_location_id: str | None
    coverage_note: str
    full_store_coverage: bool
    preview_as_of: datetime
    book_quantity: None
    book_difference: None
    inventory_accuracy: None
    difference_status: str
    observation_count: int
    observations: list[dict]
    groups: list[dict]
    conversion_receipts: list[dict]
    review_receipts: list[dict]
    reviewer_id: str | None
    review_current: bool
    disclaimer: str


class ConversionReceiptView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receipt_ref: str
    session_id: str
    session_version: int
    status: str
    product_id: str
    source_unit: str
    target_unit: str
    factor: str
    evidence_media_id: str
    effective_from: datetime
    expires_at: datetime
    verified_at: datetime
    verified_by: str
    scope: str
    ledger_write: bool


class ReviewReceiptView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receipt_ref: str
    session_id: str
    session_version: int
    decision: str
    reason: str
    observation_ids: list[str]
    reviewer_id: str
    reviewed_at: datetime
    coverage_kind: str
    coverage_location_id: str | None
    full_store_coverage: bool
    book_difference: None
    stock_adjustment: None
    ledger_write: bool


class BlindSessionEnvelope(ApiEnvelope):
    data: BlindSessionView | None


class ReviewPreviewEnvelope(ApiEnvelope):
    data: ReviewPreviewView | None


class ConversionReceiptEnvelope(ApiEnvelope):
    data: ConversionReceiptView | None


class ReviewReceiptEnvelope(ApiEnvelope):
    data: ReviewReceiptView | None
