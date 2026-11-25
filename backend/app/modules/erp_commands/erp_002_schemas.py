"""ERP-002 strict DTOs: business parameters never carry URL, SQL or credentials."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field
from backend.app.core.errors import ApiEnvelope
from backend.app.core.iso_datetime import parse_bounded_iso_offset


def _iso(value):
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


IsoDatetime = Annotated[datetime, BeforeValidator(_iso)]


def _parse_deadline(value):
    return parse_bounded_iso_offset(value, invalid_offset="erp_deadline_utc_offset_invalid")


DeadlineDatetime = Annotated[datetime, BeforeValidator(_parse_deadline)]


class StrictDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ERPCommandPropose(StrictDTO):
    # Trusted host route/service chooses action and target identity; the
    # model's only write-like contribution is a bounded business parameter.
    action: Literal["catalog_field_proposal", "purchase_document_proposal",
                    "sales_return_proposal", "sales_document_proposal", "goods_receipt_document_proposal", "catalog_maintenance_proposal"]
    target_id: str = Field(min_length=1, max_length=36)
    target_version: int = Field(ge=1)
    source_sha256: str = Field(min_length=64, max_length=64)
    business_parameters: dict
    expires_at: DeadlineDatetime


class ERPCommandDecide(StrictDTO):
    request_digest: str = Field(min_length=64, max_length=64)
    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=1, max_length=1000)
    expires_at: DeadlineDatetime


class ERPCommandStage(StrictDTO):
    request_digest: str = Field(min_length=64, max_length=64)
    approval_id: str = Field(min_length=1, max_length=36)
    expected_target_version: int = Field(ge=1)


class ERPCommandOutcomeInput(StrictDTO):
    # Host-only, deliberately NOT exposed as a public API endpoint.
    outcome: Literal["unknown", "manual_rejected", "verified_external"]
    evidence_ref: str = Field(min_length=1, max_length=200)
    evidence_sha256: str = Field(min_length=64, max_length=64)


class ERPRequestDetails(StrictDTO):
    action: Literal["catalog_field_proposal", "purchase_document_proposal",
                    "sales_return_proposal", "sales_document_proposal", "goods_receipt_document_proposal", "catalog_maintenance_proposal"]
    target_id: str
    target_version: int
    requested_by: str
    source_sha256: str
    business_parameters: dict[str, str]


class ERPApprovalReceipt(StrictDTO):
    id: str
    approved_by: str
    decision: Literal["approved", "rejected"]
    reason: str
    request_digest: str
    expires_at: IsoDatetime


class ERPCommandOut(StrictDTO):
    # The six-field contract belongs to the outer envelope, not its data.
    id: str
    state: Literal["pending", "approved", "rejected", "staged", "unknown",
                   "manual_rejected", "verified_external"]
    request_digest: str
    target_digest: str
    expires_at: IsoDatetime
    replayed: bool = False
    request: ERPRequestDetails
    approval: ERPApprovalReceipt | None


class ERPCommandEnvelope(ApiEnvelope):
    data: ERPCommandOut | None = None
