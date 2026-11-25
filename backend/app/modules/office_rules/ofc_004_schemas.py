"""OFC-004 employee-only strict DTO; approvals never extend SALE-001 limits."""
from datetime import datetime
from typing import Annotated, Literal
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field
from backend.app.core.errors import ApiEnvelope


def _iso_datetime(value):
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


IsoDatetime = Annotated[datetime, BeforeValidator(_iso_datetime)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RuleSubmit(_Strict):
    rule_key: str = Field(min_length=1, max_length=120)
    version: int = Field(ge=1)
    kind: Literal["customer_mapping", "supplier_mapping", "package_conversion",
                  "return_policy", "exchange_policy"]
    scope_key: str = Field(min_length=1, max_length=200)
    rule_body: dict
    source_ref: str = Field(min_length=1, max_length=200)
    source_sha256: str = Field(min_length=64, max_length=64)
    valid_from: IsoDatetime
    valid_until: IsoDatetime | None = None


class RuleDecide(_Strict):
    input_sha256: str = Field(min_length=64, max_length=64)
    expected_version: int = Field(ge=0, le=2)
    decision: Literal["approved", "rejected", "revoked"]
    reason: str = Field(min_length=1, max_length=2000)


class RuleOut(_Strict):
    id: str
    rule_key: str
    version: int
    kind: str
    scope_key: str
    body_sha256: str
    source_ref: str
    source_sha256: str
    valid_from: datetime
    valid_until: datetime | None
    decision_state: str
    decision_version: int
    submitted_by: str
    replayed: bool
    effect: Literal["candidate_only"] = "candidate_only"


class DecisionOut(_Strict):
    id: str
    rule_id: str
    rule_version: int
    version: int
    decision: str
    input_sha256: str
    reason: str
    decided_by: str
    replayed: bool
    effect: Literal["review_only"] = "review_only"


class ResolutionOut(_Strict):
    status: Literal["resolved", "needs_human_conflict", "unknown_or_unapproved"]
    kind: str
    scope_key: str
    rule_id: str | None
    rule_version: int | None
    source_ref: str | None
    source_sha256: str | None
    input_sha256: str | None
    decision_evidence: str | None
    explanation: str
    pending_human: bool
    converted_quantity: str | None = None
    effect: Literal["candidate_only"] = "candidate_only"


class RuleEnvelope(ApiEnvelope):
    data: RuleOut | None = None


class DecisionEnvelope(ApiEnvelope):
    data: DecisionOut | None = None


class ResolutionEnvelope(ApiEnvelope):
    data: ResolutionOut | None = None
