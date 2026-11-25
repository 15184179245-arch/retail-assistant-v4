"""OFC-006 B2a: strict request/response DTOs for the fixed ``material_review.v1`` flow.

Pure data contracts. Nothing here authorizes a caller, commits, hashes inputs,
calls a domain service, or reaches the network: capability checks, "who may act
now", version CAS, evidence re-verification, and the fixed
``extract_evidence`` / ``tasks.start|verify|close`` calls belong to the B2
service. ``action="perform"`` in a proof only states that a human asked for the
fixed service named by its step; it grants no authority by itself, and a receipt
DTO never proves that a review happened or that a business action succeeded.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, StringConstraints,
    TypeAdapter, ValidationError, field_validator, model_validator,
)

from backend.app.core.errors import ApiEnvelope

STEP_KINDS = ("intake", "extraction", "review", "approval", "execution",
              "verification", "closure")
PARENT_STATES = ("active", "paused", "needs_human", "failed", "closed")
STEP_STATES = ("pending", "completed", "failed", "unknown")
TASK_STATES = ("suggested", "accepted", "assigned", "in_progress", "verified", "closed")
TASK_DECISION_STATES = ("pending", "accepted", "rejected", "deferred", "info_requested")
APPROVAL_STATES = ("requested", "approved", "rejected", "cancelled", "expired")
RECEIPT_OPERATIONS = ("create", "advance", "pause", "resume", "fail", "mark_unknown")
TEMPLATE_KEY = "material_review.v1"
TEMPLATE_VERSION = 1
EXECUTION_MODE = "manual_task"
EFFECT = "workflow_tracking_only"
APPROVAL_SUBJECT_TYPE = "office_workflow"
APPROVAL_ACTION_KEY = "office.workflow.execute.v1"

# A pending flow step must be ``pending`` while active/paused, ``unknown`` while
# the flow waits for a human, and ``failed`` after a recorded failure.
_CURRENT_STEP_STATE = {"active": "pending", "paused": "pending",
                       "needs_human": "unknown", "failed": "failed"}

REFERENCES_MAX_BYTES = 65536
_AWARE_DATETIME_JSON = TypeAdapter(AwareDatetime)

# StringConstraints patterns use search semantics, so every pattern is anchored
# and the digest also pins an exact length: "x" * 65 or "has space" must fail.
Identifier = Annotated[str, StringConstraints(
    strict=True, min_length=1, max_length=36,
    pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
Digest = Annotated[str, StringConstraints(
    strict=True, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
StepKind = Literal["intake", "extraction", "review", "approval", "execution",
                   "verification", "closure"]
PositiveVersion = Annotated[int, Field(gt=0, strict=True)]
StepNumber = Annotated[int, Field(ge=1, le=7, strict=True)]
NextStepNumber = Annotated[int, Field(ge=1, le=8, strict=True)]
BlockingCode = Annotated[str, StringConstraints(
    strict=True, min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class _WorkflowConstants(_StrictModel):
    """Frozen template constants shared by every response DTO."""

    template_key: Literal["material_review.v1"] = TEMPLATE_KEY
    template_version: Literal[1] = TEMPLATE_VERSION
    execution_mode: Literal["manual_task"] = EXECUTION_MODE
    effect: Literal["workflow_tracking_only"] = EFFECT

    @field_validator("template_version", mode="before")
    @classmethod
    def _strict_template_version(cls, value: object) -> object:
        # Literal[1] compares by equality, so True/1.0 could slip through.
        if type(value) is not int or value != TEMPLATE_VERSION:
            raise ValueError(f"template_version must be the integer {TEMPLATE_VERSION}")
        return value


def _trim_reason(value: object) -> object:
    if not isinstance(value, str):
        raise ValueError("reason must be a string")
    trimmed = value.strip()
    if not trimmed or len(trimmed) > 1000:
        raise ValueError("reason must be non-blank text of at most 1000 characters")
    return trimmed


def _canonical_reference_bytes(value: dict[str, JsonValue]) -> bytes:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                         sort_keys=True, separators=(",", ":"))
    return encoded.encode("utf-8")


# --------------------------------------------------------------------------- #
# Request DTOs
# --------------------------------------------------------------------------- #
class WorkflowResponsibility(_StrictModel):
    kind: StepKind
    subject_id: Identifier


class WorkflowCreateInput(_StrictModel):
    template_key: Literal["material_review.v1"] = TEMPLATE_KEY
    template_version: Literal[1] = TEMPLATE_VERSION
    execution_mode: Literal["manual_task"] = EXECUTION_MODE
    task_id: Identifier
    task_context_digest: Digest
    evidence_id: Identifier
    evidence_digest: Digest
    responsibilities: list[WorkflowResponsibility] = Field(min_length=7, max_length=7)
    rule_id: Identifier | None = None
    rule_digest: Digest | None = None
    origin_run_id: Identifier | None = None

    @field_validator("template_version", mode="before")
    @classmethod
    def _strict_template_version(cls, value: object) -> object:
        if type(value) is not int or value != TEMPLATE_VERSION:
            raise ValueError(f"template_version must be the integer {TEMPLATE_VERSION}")
        return value

    @model_validator(mode="after")
    def _fixed_roles(self) -> "WorkflowCreateInput":
        if len(self.responsibilities) != len(STEP_KINDS):
            raise ValueError("responsibilities must list exactly seven roles")
        for index, item in enumerate(self.responsibilities, start=1):
            if item.kind != STEP_KINDS[index - 1]:
                raise ValueError(
                    f"responsibility {index} must be {STEP_KINDS[index - 1]!r}")
        if (self.rule_id is None) != (self.rule_digest is None):
            raise ValueError("rule_id and rule_digest must be both set or both omitted")
        return self


class IntakeProof(_StrictModel):
    kind: Literal["intake"]
    receipt_id: Identifier


class ExtractionProof(_StrictModel):
    kind: Literal["extraction"]
    action: Literal["observe", "perform"] = "observe"
    extraction_id: Identifier | None = None
    content_digest: Digest | None = None

    @model_validator(mode="after")
    def _action_binding(self) -> "ExtractionProof":
        if self.action == "observe":
            if self.extraction_id is None or self.content_digest is None:
                raise ValueError("observe requires extraction_id and content_digest")
        elif self.extraction_id is not None or self.content_digest is not None:
            raise ValueError(
                "perform must not carry client parsing results; the server calls "
                "the fixed local extraction service from its own source")
        return self


class ReviewProof(_StrictModel):
    kind: Literal["review"]
    extraction_id: Identifier
    review_digest: Digest


class ApprovalProof(_StrictModel):
    kind: Literal["approval"]
    approval_id: Identifier


class TaskProof(_StrictModel):
    """A fixed Task step; ``perform`` maps only to tasks.start/verify/close."""

    kind: Literal["execution", "verification", "closure"]
    task_id: Identifier
    task_state_digest: Digest
    action: Literal["observe", "perform"] = "observe"


AdvanceProof = Annotated[
    Union[IntakeProof, ExtractionProof, ReviewProof, ApprovalProof, TaskProof],
    Field(discriminator="kind")]


class WorkflowAdvanceInput(_StrictModel):
    expected_version: PositiveVersion
    expected_input_digest: Digest
    proof: AdvanceProof


class WorkflowControlInput(_StrictModel):
    operation: Literal["pause", "resume", "fail", "mark_unknown"]
    expected_version: PositiveVersion
    expected_input_digest: Digest
    reason: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=1000)]

    @field_validator("reason", mode="after")
    @classmethod
    def _reason(cls, value: str) -> str:
        return str(_trim_reason(value))


class WorkflowApprovalInput(_StrictModel):
    expected_version: PositiveVersion
    expected_input_digest: Digest
    expires_at: AwareDatetime
    reason: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=1000)]

    @field_validator("expires_at", mode="before")
    @classmethod
    def _http_datetime(cls, value):
        # FastAPI validates an already-decoded Python dict, unlike callers of
        # model_validate_json. Parse this JSON string with the same aware,
        # strict datetime adapter; never enable lax coercion for other fields
        # or accept numeric Unix timestamps as approval deadlines.
        if isinstance(value, str):
            if len(value) > 64:
                raise ValueError("expires_at must be a bounded timezone-aware datetime")
            try:
                return _AWARE_DATETIME_JSON.validate_json(json.dumps(value), strict=True)
            except ValidationError:
                raise ValueError("expires_at must be a timezone-aware datetime") from None
        if not isinstance(value, datetime):
            raise ValueError("expires_at must be a timezone-aware datetime")
        return value

    @field_validator("reason", mode="after")
    @classmethod
    def _reason(cls, value: str) -> str:
        return str(_trim_reason(value))


# --------------------------------------------------------------------------- #
# Response DTOs
# --------------------------------------------------------------------------- #
class WorkflowContext(_WorkflowConstants):
    task_id: Identifier
    task_context_digest: Digest
    task_state_digest: Digest
    task_state: Literal["suggested", "accepted", "assigned", "in_progress",
                        "verified", "closed"]
    task_decision_state: Literal["pending", "accepted", "rejected", "deferred",
                                 "info_requested"]
    task_assignee_id: Identifier | None = None
    evidence_id: Identifier
    evidence_digest: Digest
    rule_id: Identifier | None = None
    rule_digest: Digest | None = None
    origin_run_id: Identifier | None = None

    @model_validator(mode="after")
    def _rule_pair(self) -> "WorkflowContext":
        if (self.rule_id is None) != (self.rule_digest is None):
            raise ValueError("rule_id and rule_digest must be both set or both omitted")
        return self


class WorkflowStepReceipt(_StrictModel):
    step_no: StepNumber
    kind: StepKind
    responsible_id: Identifier
    state: Literal["pending", "completed", "failed", "unknown"]
    version: PositiveVersion
    input_digest: Digest | None = None
    output_digest: Digest | None = None
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _step_binding(self) -> "WorkflowStepReceipt":
        if self.kind != STEP_KINDS[self.step_no - 1]:
            raise ValueError(f"step {self.step_no} must be {STEP_KINDS[self.step_no - 1]!r}")
        if self.state == "completed":
            if self.input_digest is None or self.output_digest is None:
                raise ValueError("completed step requires input_digest and output_digest")
            if self.completed_at is None:
                raise ValueError("completed step requires completed_at")
        elif self.completed_at is not None:
            raise ValueError("only a completed step may carry completed_at")
        return self


class WorkflowCommandReceipt(_StrictModel):
    id: Identifier
    workflow_id: Identifier
    step_no: StepNumber | None = None
    operation: Literal["create", "advance", "pause", "resume", "fail", "mark_unknown"]
    workflow_version: PositiveVersion
    request_digest: Digest
    input_digest: Digest
    output_digest: Digest
    actor_id: Identifier
    references: dict[str, JsonValue]
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _command_binding(self) -> "WorkflowCommandReceipt":
        if self.operation == "advance":
            if self.step_no is None:
                raise ValueError("an advance command must name a step 1..7")
        elif self.step_no is not None:
            raise ValueError("only an advance command may name a step")
        try:
            size = len(_canonical_reference_bytes(self.references))
        except (TypeError, ValueError) as exc:
            raise ValueError("references must be canonical JSON without NaN") from exc
        if size > REFERENCES_MAX_BYTES:
            raise ValueError(f"references must encode to at most {REFERENCES_MAX_BYTES} UTF-8 bytes")
        return self


class WorkflowReceipt(_WorkflowConstants):
    id: Identifier
    state: Literal["active", "paused", "needs_human", "failed", "closed"]
    version: PositiveVersion
    next_step_no: NextStepNumber
    input_digest: Digest
    task_id: Identifier
    task_context_digest: Digest
    source_evidence_id: Identifier
    source_evidence_digest: Digest
    rule_id: Identifier | None = None
    rule_digest: Digest | None = None
    origin_run_id: Identifier | None = None
    context: WorkflowContext | None = None
    blocking_code: BlockingCode | None = None
    next_input_digest: Digest | None = None
    steps: list[WorkflowStepReceipt] = Field(min_length=7, max_length=7)
    review_digest: Digest | None = None
    approval_input_digest: Digest | None = None
    command_receipt: WorkflowCommandReceipt | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    closed_at: AwareDatetime | None = None
    replayed: bool = False

    @model_validator(mode="after")
    def _flow_consistency(self) -> "WorkflowReceipt":
        if (self.rule_id is None) != (self.rule_digest is None):
            raise ValueError("rule_id and rule_digest must be both set or both omitted")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if len(self.steps) != len(STEP_KINDS):
            raise ValueError("steps must list exactly seven receipts")
        for index, item in enumerate(self.steps, start=1):
            if item.step_no != index or item.kind != STEP_KINDS[index - 1]:
                raise ValueError("steps must be ordered 1..7 with the fixed kinds")
        if self.blocking_code is None and self.context is None:
            raise ValueError("context is required when blocking_code is absent")
        if self.context is not None:
            if self.context.task_id != self.task_id:
                raise ValueError("context.task_id must match the workflow task")
            if self.context.evidence_id != self.source_evidence_id:
                raise ValueError("context.evidence_id must match the workflow evidence")
            if self.context.rule_id != self.rule_id or self.context.origin_run_id != self.origin_run_id:
                raise ValueError("context provenance references must match the workflow")
            if self.blocking_code is None and (
                    self.context.task_context_digest != self.task_context_digest
                    or self.context.evidence_digest != self.source_evidence_digest
                    or self.context.rule_digest != self.rule_digest):
                raise ValueError("changed context digests require an explicit blocking code")
        if self.closed_at is None:
            if self.state == "closed":
                raise ValueError("closed workflows require closed_at")
            if self.next_step_no > len(STEP_KINDS):
                raise ValueError("an open workflow cannot point past the last step")
            if self.next_input_digest is None and self.blocking_code is None:
                raise ValueError("next_input_digest is required unless a blocking_code explains it")
            for item in self.steps:
                if item.step_no < self.next_step_no and item.state != "completed":
                    raise ValueError(f"step {item.step_no} must be completed before step {self.next_step_no}")
                if item.step_no > self.next_step_no and item.state != "pending":
                    raise ValueError(f"step {item.step_no} cannot advance past step {self.next_step_no}")
            expected = _CURRENT_STEP_STATE[self.state]
            if self.steps[self.next_step_no - 1].state != expected:
                raise ValueError(f"{self.state} requires step {self.next_step_no} to be {expected}")
        else:
            if self.state != "closed":
                raise ValueError("only a closed workflow may carry closed_at")
            if self.next_step_no != len(STEP_KINDS) + 1:
                raise ValueError("closed workflows must sit after the seventh step")
            if self.next_input_digest is not None:
                raise ValueError("closed workflows carry no next_input_digest")
            if any(item.state != "completed" for item in self.steps):
                raise ValueError("closed workflows require all seven steps completed")
            if not (self.created_at <= self.closed_at <= self.updated_at):
                raise ValueError("closed_at must fall between created_at and updated_at")
        if self.command_receipt is not None:
            if self.command_receipt.workflow_id != self.id:
                raise ValueError("command_receipt must belong to this workflow")
            if self.command_receipt.workflow_version > self.version:
                raise ValueError("command_receipt cannot come from a future workflow version")
        return self


class WorkflowApprovalReceipt(_WorkflowConstants):
    """A requested/decided approval binding; it grants no execution authority."""

    workflow_id: Identifier
    approval_id: Identifier
    input_digest: Digest
    subject_type: Literal["office_workflow"] = APPROVAL_SUBJECT_TYPE
    subject_id: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
    action_key: Literal["office.workflow.execute.v1"] = APPROVAL_ACTION_KEY
    state: Literal["requested", "approved", "rejected", "cancelled", "expired"]
    expires_at: AwareDatetime
    replayed: bool = False


# --------------------------------------------------------------------------- #
# Envelopes (six-field ApiEnvelope, data narrowed)
# --------------------------------------------------------------------------- #
class WorkflowEnvelope(ApiEnvelope):
    data: WorkflowReceipt | None


class WorkflowContextEnvelope(ApiEnvelope):
    data: WorkflowContext | None


class WorkflowApprovalEnvelope(ApiEnvelope):
    data: WorkflowApprovalReceipt | None


class WorkflowReceiptPage(_WorkflowConstants):
    workflow_id: Identifier
    workflow_version: PositiveVersion
    receipts: list[WorkflowCommandReceipt] = Field(max_length=100)
    next_after_version: PositiveVersion | None = None

    @model_validator(mode="after")
    def _history_binding(self):
        versions = [row.workflow_version for row in self.receipts]
        if versions != sorted(set(versions)) or len({row.id for row in self.receipts}) != len(self.receipts):
            raise ValueError("receipt history must be unique and ordered by workflow version")
        if any(row.workflow_id != self.workflow_id or row.workflow_version > self.workflow_version
               for row in self.receipts):
            raise ValueError("receipt history must match the current workflow and its version")
        if self.next_after_version is not None and (
                not versions or self.next_after_version != versions[-1]
                or self.next_after_version >= self.workflow_version):
            raise ValueError("history cursor must be the last returned workflow version")
        return self


class WorkflowReceiptPageEnvelope(ApiEnvelope):
    data: WorkflowReceiptPage | None
