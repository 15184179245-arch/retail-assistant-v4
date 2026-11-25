"""Service-layer DTOs for price provenance, quotes and approval binding (SALE-001)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PriceSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_type: str = Field(min_length=1, max_length=40)
    source_ref: str = Field(min_length=1, max_length=200)
    source_version: str = Field(min_length=1, max_length=120)
    currency: str = Field(min_length=3, max_length=3)
    observed_at: datetime
    expires_at: datetime | None = None

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, value: str) -> str:
        if value != value.upper():
            raise ValueError("currency_invalid")
        return value

    @field_validator("observed_at", "expires_at")
    @classmethod
    def aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone_required")
        return value.astimezone(timezone.utc)


class QuoteLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_type: str = Field(min_length=1, max_length=64)
    object_id: str = Field(min_length=1, max_length=200)
    object_version: str = Field(min_length=1, max_length=100)
    product_id: str = Field(min_length=1, max_length=200)
    quantity: Decimal

    @field_validator("quantity")
    @classmethod
    def positive_quantity(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0 or value > Decimal("1000000"):
            raise ValueError("quantity_invalid")
        if value.as_tuple().exponent < -6:
            raise ValueError("quantity_precision_invalid")
        return value


class CostSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: Decimal
    source_type: str = Field(min_length=1, max_length=40)
    source_ref: str = Field(min_length=1, max_length=200)
    observed_at: datetime

    @field_validator("amount")
    @classmethod
    def nonnegative(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("cost_invalid")
        return value

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone_required")
        return value.astimezone(timezone.utc)


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote_key: str = Field(min_length=1, max_length=200)
    rule_key: str = Field(min_length=1, max_length=120)
    line: QuoteLine
    discount_bps: int = Field(ge=0, le=10000)
    price_source: PriceSourceInput


class QuoteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    quote_key: str
    rule_key: str
    rule_version: int
    currency: str
    amount_precision: int
    rounding: str
    object_type: str
    object_id: str
    object_version: str
    product_id: str
    quantity: str
    list_price: str
    unit_price: str
    discount_bps: int
    final_amount: str
    price_source_type: str
    price_source_ref: str
    price_source_version: str
    source_observed_at: datetime
    source_expires_at: datetime | None
    status: Literal["quoted", "approval_required"]
    requires_approval: bool
    expires_at: datetime
    snapshot_digest: str
    created_at: datetime
    cost_amount: str | None = None
    cost_source_type: str | None = None
    cost_source_ref: str | None = None
    cost_observed_at: datetime | None = None


class BindApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote_id: str = Field(min_length=1, max_length=36)
    approval_id: str = Field(min_length=1, max_length=36)


class ApprovalBindingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    quote_id: str
    approval_id: str
    object_type: str
    object_id: str
    object_version: str
    snapshot_digest: str
    rule_version: int
    state: Literal["bound", "revoked"]
    expires_at: datetime
    last_validated_at: datetime | None


class RevalidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding_id: str = Field(min_length=1, max_length=36)
    object_version: str = Field(min_length=1, max_length=100)


class RevalidationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding_id: str
    quote_id: str
    object_version: str
    valid: bool
    revalidated_at: datetime
    expires_at: datetime


class QuoteContextV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["2"] = "2"
    catalog_product_id: str
    source_system: str
    external_code: str
    source_category: Literal["catalog"] = "catalog"
    product_name: str
    unit: str
    currency: str
    list_price: str
    product_version: int
    snapshot_id: str
    source_version: str
    observed_at: str
    captured_at: str
    payload_sha256: str
    policy_id: str
    policy_version: int
    policy_digest: str
    rule_id: str
    rule_version: int
    rule_digest: str
    expires_at: str
    context_digest: str


class QuoteResultV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["2"] = "2"
    id: str
    draft_id: str
    quote_key: str
    line_key: str
    catalog_product_id: str
    context: QuoteContextV2
    quantity: str
    discount_bps: int
    unit_price: str
    final_amount: str
    status: Literal["quoted", "approval_required"]
    snapshot_digest: str
    created_at: str
    expires_at: str
