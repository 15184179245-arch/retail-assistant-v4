"""Manual evidence and awaiting-arrival DTOs; never a purchase order."""
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.core.errors import ApiEnvelope


class PurchaseIntentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_kind: Literal["wechat_paste", "delivery_note_text"]
    supplier_name: str = Field(min_length=1, max_length=200)
    original_text: str = Field(min_length=1, max_length=10000)
    consent_confirmed: bool
    line_descriptions: list[str] = Field(min_length=1, max_length=30)

    @field_validator("supplier_name", "original_text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("purchase_blank")
        return value

    @field_validator("line_descriptions")
    @classmethod
    def valid_descriptions(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 500 for value in values):
            raise ValueError("purchase_line_description_invalid")
        return values


class PurchaseLineConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    product_id: str = Field(min_length=1, max_length=36)
    product_version: int = Field(ge=1)
    quantity: Decimal = Field(gt=0, le=1000000000, decimal_places=3)

    @field_validator("quantity")
    @classmethod
    def finite_quantity(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("purchase_quantity_invalid")
        return value


class PurchaseEvidenceOut(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: str
    source_kind: Literal["wechat_paste", "delivery_note_text"]
    supplier_name: str
    original_text: str
    content_sha256: str
    consent_confirmed: bool
    created_by: str
    created_at: datetime


class PurchaseLineOut(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: str
    original_description: str
    product_id: str | None
    product_version: int | None
    quantity: str | None
    confirmed_by: str | None
    confirmed_at: datetime | None


class PurchaseIntentOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    organization_id: str
    store_id: str
    creator_id: str
    status: Literal["awaiting_arrival"]
    version: int
    created_at: datetime
    updated_at: datetime
    evidence: PurchaseEvidenceOut
    lines: list[PurchaseLineOut]


class PurchaseIntentEnvelope(ApiEnvelope):
    data: PurchaseIntentOut | None


class PurchaseIntentListEnvelope(ApiEnvelope):
    data: list[PurchaseIntentOut] | None
