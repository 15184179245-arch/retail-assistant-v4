"""Sales draft DTOs; stage/status and sync/handoff remain separate axes."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.core.errors import ApiEnvelope


class DraftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: str = Field(min_length=3, max_length=3)
    quote_id: str | None = Field(default=None, max_length=36)

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, value: str) -> str:
        if value != value.upper():
            raise ValueError("currency_invalid")
        return value


class LineCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote_id: str | None = Field(default=None, max_length=36)
    line_key: str = Field(min_length=1, max_length=200)
    product_id: str = Field(min_length=1, max_length=200)
    product_name: str = Field(min_length=1, max_length=200)
    quantity: Decimal
    unit_price: Decimal
    discount_bps: int = Field(ge=0, le=10000)

    @field_validator("quantity", "unit_price")
    @classmethod
    def positive_decimal(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0 or value > Decimal("1000000000"):
            raise ValueError("amount_invalid")
        if value.as_tuple().exponent < -6:
            raise ValueError("amount_precision_invalid")
        return value


class HandoffCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_ref: str | None = Field(default=None, max_length=200)


class ReceiptCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_ref: str = Field(min_length=1, max_length=200)
    paid: bool = False
    unknown: bool = False


class DraftLineOut(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: str
    quote_id: str | None = Field(default=None, max_length=36)
    line_key: str
    product_id: str
    product_name: str
    quantity: str
    unit_price: str
    discount_bps: int
    line_amount: str
    confirmed_by: str | None
    confirmed_at: datetime | None


class HandoffOut(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: str
    handoff_state: Literal["requested", "sent", "receipt_unknown", "confirmed", "failed"]
    external_ref: str | None
    created_at: datetime
    updated_at: datetime


class DraftOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    organization_id: str
    store_id: str
    creator_id: str
    quote_id: str | None
    stage: Literal["draft", "ready"]
    status: Literal["pending", "paid"]
    sync_state: Literal["local_only", "pending", "synced", "conflict"]
    version: int
    currency: str
    total_amount: str
    voided_at: datetime | None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    lines: list[DraftLineOut] = []
    handoffs: list[HandoffOut] = []


class DraftEnvelope(ApiEnvelope):
    data: DraftOut | None


class DraftListEnvelope(ApiEnvelope):
    data: list[DraftOut] | None


class HandoffEnvelope(ApiEnvelope):
    data: HandoffOut | None


from backend.app.modules.pricing.schemas import PriceSourceInput, QuoteResponse

class LineQuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote_key: str = Field(min_length=1, max_length=200)
    rule_key: str = Field(min_length=1, max_length=120)
    product_id: str = Field(min_length=1, max_length=200)
    quantity: Decimal = Field(gt=0, le=1000000, decimal_places=6)
    discount_bps: int = Field(ge=0, le=10000)
    price_source: PriceSourceInput

class LineQuoteEnvelope(ApiEnvelope):
    data: QuoteResponse | None


from backend.app.modules.pricing.schemas import QuoteContextV2, QuoteResultV2


class LineQuoteRequestV2(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    quote_key: str = Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")
    line_key: str = Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")
    catalog_product_id: str = Field(min_length=1, max_length=36)
    context_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    quantity: str = Field(min_length=1, max_length=40)
    discount_bps: int = Field(ge=0, le=10000)

    @field_validator("quantity")
    @classmethod
    def canonical_quantity(cls, value):
        try:
            number = Decimal(value)
        except InvalidOperation as error:
            raise ValueError("quantity_invalid") from error
        if not number.is_finite() or number <= 0 or number > 1000000 or number.as_tuple().exponent < -6:
            raise ValueError("quantity_invalid")
        return format(number.normalize(), "f")


class LineBindRequestV2(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    line_key: str = Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")
    quote_key: str = Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")
    quote_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_selection(cls, value):
        if value is not True:
            raise ValueError("explicit_confirmation_required")
        return value


class LineResultV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["2"] = "2"
    id: str
    draft_id: str
    line_key: str
    quote_key: str
    quote_id: str
    catalog_product_id: str
    unit: str
    currency: str
    quantity: str
    unit_price: str
    line_amount: str
    product_name: str
    # Selection confirmation does not grant sales.confirm or mark the draft ready.
    stage: Literal["draft"] = "draft"


class EligibilityV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    usable: bool
    code: str


class QuoteLookupV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["recorded", "unknown"]
    original_result: QuoteResultV2 | None
    current_eligibility: EligibilityV2


class LineLookupV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["recorded", "unknown"]
    original_result: LineResultV2 | None
    current_eligibility: EligibilityV2


class ContextEnvelopeV2(ApiEnvelope):
    data: QuoteContextV2 | None


class QuoteEnvelopeV2(ApiEnvelope):
    data: QuoteLookupV2 | None


class LineEnvelopeV2(ApiEnvelope):
    data: LineLookupV2 | None
