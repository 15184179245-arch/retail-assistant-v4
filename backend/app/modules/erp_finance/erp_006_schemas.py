"""Strict DTOs and API envelopes for ERP-006 settlement command lifecycle."""
from decimal import Decimal
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from backend.app.core.errors import ApiEnvelope


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AllocationLineProposal(Strict):
    line_id: str = Field(min_length=1, max_length=120)
    document_ref: str = Field(min_length=1, max_length=120)
    allocated_amount: str = Field(min_length=1, max_length=32)
    note: str | None = Field(default=None, max_length=255)

    @field_validator("allocated_amount")
    @classmethod
    def validate_amount(cls, value: str) -> str:
        try:
            d = Decimal(value)
            if d <= Decimal("0"):
                raise ValueError("allocated_amount must be positive")
            # Limit to 2 decimal places
            if d.as_tuple().exponent < -2:
                raise ValueError("allocated_amount exceeds 2 decimal places")
        except Exception as exc:
            raise ValueError(f"invalid decimal amount: {exc}") from exc
        return str(d)


class SettlementProposalCreate(Strict):
    workpaper_id: str = Field(min_length=36, max_length=36)
    expected_workpaper_version: int = Field(ge=1)
    operation: Literal["receipt_allocation", "payment_allocation"]
    party_type: Literal["customer", "supplier"]
    party_ref: str = Field(min_length=1, max_length=120)
    currency: Literal["CNY", "USD"]
    settlement_amount: str = Field(min_length=1, max_length=32)
    lines: list[AllocationLineProposal] = Field(min_length=1, max_length=500)
    proposal_key: str = Field(min_length=1, max_length=200)

    @field_validator("settlement_amount")
    @classmethod
    def validate_total_amount(cls, value: str) -> str:
        try:
            d = Decimal(value)
            if d <= Decimal("0"):
                raise ValueError("settlement_amount must be positive")
            if d.as_tuple().exponent < -2:
                raise ValueError("settlement_amount exceeds 2 decimal places")
        except Exception as exc:
            raise ValueError(f"invalid decimal amount: {exc}") from exc
        return str(d)


class SettlementApprovalCreate(Strict):
    command_id: str = Field(min_length=36, max_length=36)
    decision: Literal["approved", "rejected"]
    expected_workpaper_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=255)


class SettlementStageInput(Strict):
    command_id: str = Field(min_length=36, max_length=36)
    queue_key: str = Field(min_length=1, max_length=200)


class SettlementDispatchInput(Strict):
    attempt_id: str = Field(min_length=36, max_length=36)


class SettlementReconcileInput(Strict):
    attempt_id: str = Field(min_length=36, max_length=36)


class SettlementCommandOut(Strict):
    id: str
    organization_id: str
    store_id: str
    workpaper_id: str
    request_id: str
    route: str
    operation: str
    party_type: str
    party_ref: str
    currency: str
    settlement_amount: str
    source_version: int
    source_digest: str
    proposal_key: str
    proposal_digest: str
    created_by: str
    created_at: str


class SettlementAttemptOut(Strict):
    id: str
    organization_id: str
    store_id: str
    command_id: str
    intent_id: str
    queue_key: str
    state: str
    version: int
    allocation_results: list[dict[str, Any]]
    delivery_ref: str | None
    external_ref: str | None
    receipt_digest: str | None
    created_at: str
    updated_at: str
    dispatched_at: str | None


class SettlementEnvelope(ApiEnvelope):
    data: dict[str, Any] | list[Any] | None = None
