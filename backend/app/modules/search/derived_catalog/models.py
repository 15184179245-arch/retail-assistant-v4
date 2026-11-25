"""Strict data contracts for the parallel derived catalog layer.

Every model uses `extra="forbid"` and `strict=True`. Derived attributes are
never authoritative business data; they are revocable, source-anchored clues
that preserve exact Unicode offsets (`source_text[start:end] == value`),
snapshot SHA256, and scope boundaries.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmptyStr = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
ScopeToken = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"),
]
Sha256Hex = Annotated[
    str,
    StringConstraints(strict=True, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]

DerivedKind = Literal[
    "legacy_code",
    "manufacturer_part_number",
    "specification",
    "model",
    "brand",
    "unclassified",
]

DerivedCertainty = Literal[
    "observed_field",
    "candidate",
    "ambiguous",
]

ModelDerivedCertainty = Literal[
    "candidate",
    "ambiguous",
]

SourceFieldName = Literal[
    "code",
    "name",
    "shortName",
    "spec",
    "model",
    "brandName",
    "barCode",
    "pyCode",
    "productCategoryIdName",
]

SourceFieldPath = Literal[
    "raw.raw.code",
    "raw.raw.name",
    "raw.raw.shortName",
    "raw.raw.spec",
    "raw.raw.model",
    "raw.raw.brandName",
    "raw.raw.barCode",
    "raw.raw.pyCode",
    "raw.raw.productCategoryIdName",
    "raw.code",
    "raw.name",
    "raw.spec",
    "raw.model",
    "raw.barCode",
    "raw.pyCode",
    "code",
    "name",
    "spec",
    "model",
    "barCode",
    "pyCode",
]

ExtractionStatus = Literal[
    "completed",
    "checked_no_attributes",
    "deterministic_only",
    "failed",
    "in_flight",
]

UsageStatus = Literal[
    "known",
    "unknown",
    "not_called",
    "failed",
    "in_flight",
]

OriginKind = Literal[
    "model",
    "rule",
]

RelationKind = Literal[
    "possible_shared_brand",
    "possible_same_brand_part_number",
    "possible_shared_legacy_code",
    "ambiguous_cross_brand_part_number",
    "ambiguous_shared_token",
]

NORMALIZATION_RULE_VERSION = "derived-norm-v1"
ALGORITHM_VERSION = "derived-catalog-alg-v1"
PROMPT_VERSION = "derived-catalog-prompt-v1"

WHITELIST_FIELDS: tuple[SourceFieldName, ...] = (
    "code",
    "name",
    "shortName",
    "spec",
    "model",
    "brandName",
    "barCode",
    "pyCode",
    "productCategoryIdName",
)


class SourceLookupItem(BaseModel):
    """Exact source reference item passed into `read_current_terms`."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    product_id: ScopeToken
    snapshot_id: ScopeToken
    payload_sha256: Sha256Hex


class WhitelistedProductInput(BaseModel):
    """Strict whitelisted product view; never includes stock, price, supplier, or full raw."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    product_id: ScopeToken
    organization_id: ScopeToken
    store_id: ScopeToken
    snapshot_id: ScopeToken
    payload_sha256: Sha256Hex
    field_values: dict[SourceFieldPath, str]
    input_digest: Sha256Hex

    @model_validator(mode="after")
    def _validate_fields(self) -> "WhitelistedProductInput":
        for path, val in self.field_values.items():
            if not isinstance(val, str) or not val:
                raise ValueError(f"field_values[{path}] must be a non-empty string")
        return self


class ModelAttributeProposal(BaseModel):
    """Single attribute span proposed by the untrusted model for one anonymous row."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: DerivedKind
    value: NonEmptyStr
    source_field: SourceFieldPath
    start: int = Field(ge=0, le=10_000)
    end: int = Field(ge=1, le=10_000)
    certainty: ModelDerivedCertainty = "candidate"
    brand_scope: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=80)] | None = None

    @model_validator(mode="after")
    def _validate_span(self) -> "ModelAttributeProposal":
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        if self.end - self.start != len(self.value):
            raise ValueError("span length must match value length")
        return self


class ModelItemExtraction(BaseModel):
    """Untrusted model extraction for a single anonymous row reference (`row_ref`)."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    row_ref: Annotated[str, StringConstraints(strict=True, pattern=r"^r\d{1,3}$")]
    attributes: list[ModelAttributeProposal] = Field(default_factory=list, max_length=24)


class ModelBatchExtraction(BaseModel):
    """Top-level strict JSON schema returned by DeepSeek for a batch of products."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    items: list[ModelItemExtraction] = Field(min_length=1, max_length=8)


class DerivedAttributeRecord(BaseModel):
    """Verified derived attribute anchored to an exact source field substring."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    product_id: ScopeToken
    organization_id: ScopeToken
    store_id: ScopeToken
    snapshot_id: ScopeToken
    payload_sha256: Sha256Hex
    kind: DerivedKind
    value: NonEmptyStr
    source_field: SourceFieldPath
    start: int = Field(ge=0, le=10_000)
    end: int = Field(ge=1, le=10_000)
    normalized_value: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)] | None = None
    normalization_version: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)] | None = None
    certainty: DerivedCertainty
    brand_scope: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=80)] | None = None
    barcode_hint: Literal["ean13_valid", "ean13_invalid_check", "scientific_notation", "leading_zero"] | None = None
    origin: OriginKind = "model"
    extraction_version: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=160)] | None = None
    model: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=120)]
    prompt_version: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]
    algorithm_version: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]
    input_digest: Sha256Hex
    extraction_status: ExtractionStatus
    usage_status: UsageStatus
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    error_code: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=120)] | None = None

    @model_validator(mode="after")
    def _check_invariants(self) -> "DerivedAttributeRecord":
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        if self.end - self.start != len(self.value):
            raise ValueError("span length must match value length")
        if self.normalized_value is not None and self.normalization_version is None:
            raise ValueError("normalization_version required when normalized_value is set")
        return self


class ProductExtractionReceipt(BaseModel):
    """Per-product extraction watermark and provenance stored in the sidecar."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    organization_id: ScopeToken
    store_id: ScopeToken
    product_id: ScopeToken
    snapshot_id: ScopeToken
    payload_sha256: Sha256Hex
    extraction_version: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=160)]
    model: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=120)]
    prompt_version: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]
    algorithm_version: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]
    input_digest: Sha256Hex
    status: ExtractionStatus
    usage_status: UsageStatus
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    error_code: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=120)] | None = None
    call_id: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)] | None = None
    parent_call_id: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)] | None = None
    rejected_proposals: int = Field(default=0, ge=0)
    recorded_at: datetime
    attributes: list[DerivedAttributeRecord] = Field(default_factory=list)


class CandidateRelationRecord(BaseModel):
    """Cross-product candidate relation backed by source evidence; never merges products."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    organization_id: ScopeToken
    store_id: ScopeToken
    relation_kind: RelationKind
    shared_value: NonEmptyStr
    brand_scope: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=80)] | None = None
    certainty: Literal["candidate", "ambiguous"] = "candidate"
    product_ids: list[ScopeToken] = Field(min_length=2, max_length=200)
    snapshot_ids: list[ScopeToken] = Field(min_length=2, max_length=200)
    evidence_fields: list[SourceFieldPath] = Field(min_length=2, max_length=200)
    note: NonEmptyStr
