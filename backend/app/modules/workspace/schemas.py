"""Pydantic DTOs, payload validators, and 6-field ApiEnvelope wrappers for WSP-001/WSP-002."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.core.errors import ApiEnvelope

ArtifactKind = Literal[
    "quote_draft",
    "purchase_draft",
    "stocktake_review",
    "receiving_batch",
    "work_item_sheet",
]
# submitted is a sealed workspace draft, not a formal ERP/inventory transaction.
ArtifactStatus = Literal["draft", "submitted", "archived"]
ActorType = Literal["human", "agent"]
ArtifactOpType = Literal[
    "create",
    "patch",
    "undo",
    "lock",
    "unlock",
    "accept_suggestion",
    "ignore_suggestion",
    "submit",
]
StatusKind = Literal[
    "development",
    "production",
    "suggestion",
    "fact",
    "pending_sync",
    "settled",
    "verified",
    "unverified",
    "waiting",
    "error",
]

ARTIFACT_KINDS: tuple[str, ...] = (
    "quote_draft",
    "purchase_draft",
    "stocktake_review",
    "receiving_batch",
    "work_item_sheet",
)
QUOTE_ALLOWED_STAGES: tuple[str, ...] = ("draft", "ready")
QUOTE_ALLOWED_STATUSES: tuple[str, ...] = ("pending", "paid")


def validate_artifact_payload(
    kind: str,
    payload: dict[str, Any],
    *,
    apply_defaults: bool = False,
) -> dict[str, Any]:
    """Validate and normalize kind-specific payload invariants."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    normalized = dict(payload)
    for key in normalized:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("payload keys must be non-empty strings")

    if kind == "quote_draft":
        if apply_defaults:
            normalized.setdefault("stage", "draft")
            normalized.setdefault("status", "pending")
            normalized.setdefault("lines", [])
        if "stage" in normalized and normalized["stage"] not in QUOTE_ALLOWED_STAGES:
            raise ValueError(
                "quote_draft payload.stage only allows 'draft' or 'ready'; use independent fields for void/expiry/exception"
            )
        if "status" in normalized and normalized["status"] not in QUOTE_ALLOWED_STATUSES:
            raise ValueError(
                "quote_draft payload.status only allows 'pending' or 'paid'; use independent fields for void/expiry/sync"
            )
        if "lines" in normalized and not isinstance(normalized["lines"], list):
            raise ValueError("quote_draft payload.lines must be a list")
    elif kind == "purchase_draft":
        if apply_defaults:
            normalized.setdefault("lines", [])
        if "lines" in normalized and not isinstance(normalized["lines"], list):
            raise ValueError("purchase_draft payload.lines must be a list")
    elif kind == "stocktake_review":
        if apply_defaults:
            normalized.setdefault("items", [])
        if "items" in normalized and not isinstance(normalized["items"], list):
            raise ValueError("stocktake_review payload.items must be a list")
    elif kind == "receiving_batch":
        if apply_defaults:
            normalized.setdefault("items", [])
        if "items" in normalized and not isinstance(normalized["items"], list):
            raise ValueError("receiving_batch payload.items must be a list")
    elif kind == "work_item_sheet":
        if apply_defaults:
            normalized.setdefault("items", [])
        if "items" in normalized and not isinstance(normalized["items"], list):
            raise ValueError("work_item_sheet payload.items must be a list")
    else:
        raise ValueError(f"unsupported artifact kind: {kind}")

    return normalized


class PendingSuggestionRecord(BaseModel):
    op_id: str
    field: str
    suggested_value: Any
    current_value: Any = None
    reason: str | None = None
    actor_subject_id: str
    created_at: datetime


class ArtifactRecord(BaseModel):
    id: str
    organization_id: str
    store_id: str
    owner_subject_id: str
    thread_id: str
    kind: ArtifactKind
    title: str
    status: ArtifactStatus
    version: int
    payload: dict[str, Any]
    source_refs: list[dict[str, Any]]
    locked_fields: list[str]
    pending_suggestions: list[PendingSuggestionRecord]
    submitted_receipt_ref: str | None = None
    created_at: datetime
    updated_at: datetime


class ArtifactOpRecord(BaseModel):
    id: str
    artifact_id: str
    version_before: int
    version_after: int
    actor_type: ActorType
    actor_subject_id: str
    op_type: ArtifactOpType
    patch: dict[str, Any]
    applied_fields: list[str]
    skipped_fields: list[str]
    undo_of_op_id: str | None = None
    idempotency_key: str | None = None
    created_at: datetime


class ArtifactMutationData(BaseModel):
    artifact: ArtifactRecord
    op: ArtifactOpRecord
    message: str | None = None
    skipped_fields: list[str] = Field(default_factory=list)
    pending_suggestions_added: list[PendingSuggestionRecord] = Field(default_factory=list)
    idempotent_replay: bool = False


class ArtifactDetailData(BaseModel):
    artifact: ArtifactRecord
    ops: list[ArtifactOpRecord] = Field(default_factory=list)


class ArtifactListData(BaseModel):
    items: list[ArtifactRecord]


class ArtifactCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(min_length=1, max_length=64)
    kind: ArtifactKind
    title: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    actor_type: ActorType = "human"
    idempotency_key: str | None = Field(default=None, max_length=128)


class ArtifactPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(ge=1)
    patch: dict[str, Any]
    actor_type: ActorType = "human"
    reason: str | None = Field(default=None, max_length=500)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    idempotency_key: str | None = Field(default=None, max_length=128)


class ArtifactLockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(ge=1)
    fields: list[str] = Field(min_length=1)

    @field_validator("fields")
    @classmethod
    def _validate_fields(cls, value: list[str]) -> list[str]:
        cleaned = [f.strip() for f in value if isinstance(f, str) and f.strip()]
        if not cleaned:
            raise ValueError("at least one non-empty field name is required")
        return list(dict.fromkeys(cleaned))


class ArtifactUnlockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(ge=1)
    field: str | None = None
    fields: list[str] | None = None

    @model_validator(mode="after")
    def _validate_target(self) -> ArtifactUnlockRequest:
        targets: list[str] = []
        if self.field and self.field.strip():
            targets.append(self.field.strip())
        if self.fields:
            targets.extend(f.strip() for f in self.fields if isinstance(f, str) and f.strip())
        if not targets:
            raise ValueError("field or fields must specify at least one locked field")
        self.fields = list(dict.fromkeys(targets))
        return self


class ArtifactSuggestionDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(ge=1)
    field: str | None = None


class ArtifactUndoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(ge=1)
    target_op_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=128)


class ArtifactSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(ge=1)
    idempotency_key: str | None = Field(default=None, max_length=128)
    note: str | None = Field(default=None, max_length=500)


class ThreadWorkspaceRecord(BaseModel):
    thread_id: str
    organization_id: str
    store_id: str
    subject_id: str
    open_objects: list[dict[str, Any]] = Field(default_factory=list)
    active_object_ref: dict[str, Any] | None = None
    last_editor_subject_id: str | None = None
    last_editor_device: str | None = None
    last_edited_at: datetime | None = None
    recent_editor_hint: str | None = None
    version: int = 1
    updated_at: datetime | None = None


class ThreadWorkspaceUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version: int | None = Field(default=None, ge=0)
    open_objects: list[dict[str, Any]] = Field(default_factory=list)
    active_object_ref: dict[str, Any] | None = None
    device: str = Field(default="web", min_length=1, max_length=32)


class ThreadEventRecord(BaseModel):
    seq: int
    event_id: str
    thread_id: str
    subject_id: str
    artifact_id: str | None = None
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class ThreadEventsData(BaseModel):
    thread_id: str
    cursor: int
    next_cursor: int
    events: list[ThreadEventRecord]


class BlockProvenance(BaseModel):
    source: str
    synced_at: datetime | None = None
    verified_by: str | None = None
    status_kind: StatusKind
    stale_refs: list[dict[str, Any]] = Field(default_factory=list)
    stripped_fields: list[str] = Field(default_factory=list)


class ResolvedBlockRecord(BaseModel):
    type: str
    version: str
    props: dict[str, Any]
    refs: list[dict[str, Any]]
    resolved_data: dict[str, Any] = Field(default_factory=dict)
    provenance: BlockProvenance
    status_kind: StatusKind
    degraded: bool = False
    fallback_text: str | None = None
    reason: str | None = None


class BlockResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block: dict[str, Any] | None = None
    blocks: list[dict[str, Any]] | None = None
    thread_id: str | None = Field(default=None, max_length=64)
    type: str | None = None
    version: str | None = None
    props: dict[str, Any] | None = None
    refs: list[dict[str, Any]] | None = None

    def extract_blocks(self) -> list[dict[str, Any]]:
        if self.blocks is not None:
            return self.blocks
        if self.block is not None:
            return [self.block]
        if self.type is not None or self.version is not None or self.props is not None or self.refs is not None:
            candidate: dict[str, Any] = {}
            if self.type is not None:
                candidate["type"] = self.type
            if self.version is not None:
                candidate["version"] = self.version
            if self.props is not None:
                candidate["props"] = self.props
            if self.refs is not None:
                candidate["refs"] = self.refs
            return [candidate]
        return []


class BlockResolveData(BaseModel):
    block: ResolvedBlockRecord
    blocks: list[ResolvedBlockRecord]


class BlockPinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(min_length=1, max_length=64)
    block: dict[str, Any]
    kind: ArtifactKind | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    idempotency_key: str | None = Field(default=None, max_length=128)


class BlockPinData(BaseModel):
    artifact: ArtifactRecord
    resolved_block: ResolvedBlockRecord
    op: ArtifactOpRecord


class ArtifactDetailEnvelope(ApiEnvelope):
    data: ArtifactDetailData | None = None


class ArtifactListEnvelope(ApiEnvelope):
    data: ArtifactListData | None = None


class ArtifactMutationEnvelope(ApiEnvelope):
    data: ArtifactMutationData | None = None


class ThreadWorkspaceEnvelope(ApiEnvelope):
    data: ThreadWorkspaceRecord | None = None


class ThreadEventsEnvelope(ApiEnvelope):
    data: ThreadEventsData | None = None


class BlockResolveEnvelope(ApiEnvelope):
    data: BlockResolveData | None = None


class BlockPinEnvelope(ApiEnvelope):
    data: BlockPinData | None = None
