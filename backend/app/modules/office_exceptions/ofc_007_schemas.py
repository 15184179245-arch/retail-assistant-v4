"""OFC-007 B2a: strict DTOs for the exception queue, case commands and pages.

Pure data contracts. Nothing here authorizes a caller, opens a database row,
hashes an input, re-reads a source, claims an owner, transfers responsibility,
or resolves a case: capability checks, "who may act now", version CAS, source
re-reading and closure-evidence verification belong to the B2 service (owned by
the lead). A DTO that parses proves only that the wire shape is admissible; it
never proves that a case is owned, that a source problem was fixed, that a
receiving batch was booked, that an ERP document exists, or that an agent
handled anything. ``effect == "case_tracking_only"`` and
``mode == "human_supervised"`` are constants that state this explicitly.

Enum values are copied from the frozen B1 contract, not imported from the ORM:
the wire contract must not silently follow a database change.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field,
    StringConstraints, field_validator, model_validator,
)

from backend.app.core.errors import ApiEnvelope

SOURCE_TYPES = ("receiving_discrepancy", "erp_sandbox", "erp_intent",
                "sales_handoff", "workflow", "task")
CASE_KINDS = ("shortage", "overage", "wrong_item", "identity_ambiguous",
              "partial", "unknown_write", "overdue", "price_change",
              "workflow_attention")
CASE_STATES = ("open", "owned", "transfer_pending", "resolved")
OPERATIONS = ("create", "claim", "takeover", "propose_transfer",
              "accept_transfer", "cancel_transfer", "resolve", "reopen")
SOURCE_VERIFICATIONS = ("employee_observation", "synthetic_erp",
                        "manual_erp_evidence", "sales_record",
                        "workflow_tracking", "task_record")
RESOLUTION_KINDS = ("verified_resolution", "reconciled_partial",
                    "verified_rejection")
EFFECT = "case_tracking_only"
MODE = "human_supervised"

# Only the kind each real source can raise (same frozen map as B1): a source
# cannot be relabelled into an anomaly it does not produce.
_KINDS_BY_SOURCE = {
    "receiving_discrepancy": frozenset(
        {"shortage", "overage", "wrong_item", "identity_ambiguous", "price_change"}),
    "erp_sandbox": frozenset({"partial", "unknown_write"}),
    "erp_intent": frozenset({"unknown_write"}),
    "sales_handoff": frozenset({"unknown_write"}),
    "workflow": frozenset({"workflow_attention"}),
    "task": frozenset({"overdue"}),
}
# Each source can only be described by the verification its own domain records.
_VERIFICATION_BY_SOURCE = {
    "receiving_discrepancy": "employee_observation",
    "erp_sandbox": "synthetic_erp",
    "erp_intent": "manual_erp_evidence",
    "sales_handoff": "sales_record",
    "workflow": "workflow_tracking",
    "task": "task_record",
}

# StringConstraints patterns use search semantics, so every pattern is anchored
# and the digest also pins an exact length: "x" * 65 or uppercase hex must fail.
Identifier = Annotated[str, StringConstraints(
    strict=True, min_length=1, max_length=36,
    pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
Digest = Annotated[str, StringConstraints(
    strict=True, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
PositiveVersion = Annotated[int, Field(gt=0, strict=True)]
# Printable ASCII, no leading/trailing whitespace, no newline: source states are
# short machine tokens, not free prose.
StateText = Annotated[str, StringConstraints(
    strict=True, min_length=1, max_length=48,
    pattern=r"^[!-~](?:[ -~]*[!-~])?$")]
ReferenceKind = Annotated[str, StringConstraints(
    strict=True, min_length=1, max_length=48,
    pattern=r"^[!-~](?:[ -~]*[!-~])?$")]
ErrorCode = Annotated[str, StringConstraints(
    strict=True, min_length=1, max_length=64,
    pattern=r"^[!-~](?:[ -~]*[!-~])?$")]
def _no_padded_text(value: str) -> str:
    if value != value.strip() or not value.strip():
        raise ValueError("text must be non-blank without edge whitespace")
    return value


UnitText = Annotated[str, StringConstraints(strict=True, min_length=1,
                                            max_length=16),
                     AfterValidator(_no_padded_text)]
# Non-negative decimal string, at most 40 characters, never converted here.
# Digit count is deliberately not capped: real sources keep up to six decimal
# places, and this DTO must not quietly reject one of its own six sources.
Quantity = Annotated[str, StringConstraints(
    strict=True, min_length=1, max_length=40,
    pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")]
Cursor = Annotated[str, StringConstraints(strict=True, min_length=1,
                                          max_length=240)]


def _reason_text(value: str) -> str:
    if value != value.strip() or not value.strip():
        raise ValueError(
            "reason must be non-blank text without leading or trailing whitespace")
    return value


Reason = Annotated[str, StringConstraints(strict=True, min_length=1,
                                          max_length=1000),
                   AfterValidator(_reason_text)]

SourceType = Literal["receiving_discrepancy", "erp_sandbox", "erp_intent",
                     "sales_handoff", "workflow", "task"]
CaseKind = Literal["shortage", "overage", "wrong_item", "identity_ambiguous",
                   "partial", "unknown_write", "overdue", "price_change",
                   "workflow_attention"]
CaseState = Literal["open", "owned", "transfer_pending", "resolved"]
Operation = Literal["create", "claim", "takeover", "propose_transfer",
                    "accept_transfer", "cancel_transfer", "resolve", "reopen"]
CommandOperation = Literal["claim", "takeover", "propose_transfer",
                           "accept_transfer", "cancel_transfer", "resolve",
                           "reopen"]
SourceVerification = Literal["employee_observation", "synthetic_erp",
                             "manual_erp_evidence", "sales_record",
                             "workflow_tracking", "task_record"]
ResolutionKind = Literal["verified_resolution", "reconciled_partial",
                         "verified_rejection"]
BlockingCode = Literal["SOURCE_UNAVAILABLE"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True,
                              hide_input_in_errors=True)


class _CaseConstants(_StrictModel):
    """Frozen constants that deny any claim of upstream success."""

    effect: Literal["case_tracking_only"] = EFFECT
    mode: Literal["human_supervised"] = MODE


def _require_kind_for_source(source_type: str, kind: str) -> None:
    allowed = _KINDS_BY_SOURCE.get(source_type)
    if allowed is None:
        raise ValueError(f"unknown source_type {source_type!r}")
    if kind not in allowed:
        raise ValueError(f"kind {kind!r} is not a {source_type} anomaly")


class SourceLocator(_StrictModel):
    """Which real source object an exception is attached to."""

    source_type: SourceType
    source_id: Identifier
    kind: CaseKind

    @model_validator(mode="after")
    def _kind_matches_source(self) -> "SourceLocator":
        _require_kind_for_source(self.source_type, self.kind)
        return self


class SourceReference(_StrictModel):
    """One normalized pointer into a real source; never free parameters."""

    kind: ReferenceKind
    id: Identifier
    version: PositiveVersion | None = None
    digest: Digest | None = None


class SourceLine(_StrictModel):
    """One observed source line, only fields the source really recorded."""

    id: Identifier
    state: StateText
    version: PositiveVersion | None = None
    receipt_digest: Digest | None = None
    evidence_digest: Digest | None = None
    error_code: ErrorCode | None = None
    observed_quantity: Quantity | None = None
    expected_quantity: Quantity | None = None
    cumulative_observed_quantity: Quantity | None = None
    unit: UnitText | None = None
    source_verification: SourceVerification


class ExceptionSource(SourceLocator):
    """Read-only projection of a source's current verified state.

    ``source_version`` is nullable on purpose: Tasks, ERP intents and sales
    handoffs carry no integer version, and B1 forbids inventing one. The frozen
    digest is the content/event-set digest for those sources.

    ``can_resolve`` is declared by the source projection, never inferred from
    ``anomaly_present``: a partial batch can still show differences while every
    line's outcome is already verified.
    """

    source_digest: Digest
    source_version: PositiveVersion | None
    source_state: StateText
    anomaly_present: bool
    can_resolve: bool
    resolution_kind: ResolutionKind | None = None
    resolution_digest: Digest | None = None
    source_verification: SourceVerification
    references: list[SourceReference] = Field(default_factory=list, max_length=100)
    lines: list[SourceLine] = Field(default_factory=list, max_length=100)
    effect: Literal["case_tracking_only"] = EFFECT
    mode: Literal["human_supervised"] = MODE

    @field_validator("source_version", mode="before")
    @classmethod
    def _no_fabricated_version(cls, value: object) -> object:
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError("source_version must be a positive integer or null")
        return value

    @model_validator(mode="after")
    def _verification_matches_source(self) -> "ExceptionSource":
        expected = _VERIFICATION_BY_SOURCE[self.source_type]
        if self.source_verification != expected:
            raise ValueError(
                f"{self.source_type} must be verified as {expected!r}")
        return self

    @model_validator(mode="after")
    def _closure_evidence_is_all_or_nothing(self) -> "ExceptionSource":
        paired = (self.resolution_kind is not None
                  and self.resolution_digest is not None)
        if self.can_resolve is not paired:
            raise ValueError(
                "can_resolve is true exactly when resolution_kind and "
                "resolution_digest are both present")
        if not paired and (self.resolution_kind is not None
                           or self.resolution_digest is not None):
            raise ValueError("resolution evidence must be complete or absent")
        return self

    @model_validator(mode="after")
    def _bounded_unique_references(self) -> "ExceptionSource":
        if len({line.id for line in self.lines}) != len(self.lines):
            raise ValueError("line ids must be unique")
        if len({(ref.kind, ref.id) for ref in self.references}) != len(self.references):
            raise ValueError("reference (kind, id) pairs must be unique")
        return self


class CaseCreateInput(SourceLocator):
    """Open one case; there is no caller, owner or capability in the payload."""

    expected_source_digest: Digest
    expected_source_version: PositiveVersion | None
    reason: Reason


class CaseCommandInput(_StrictModel):
    """One command against a case; the actor is always the session person."""

    operation: CommandOperation
    expected_version: PositiveVersion
    expected_source_digest: Digest
    reason: Reason
    to_owner_id: Identifier | None = None
    expected_resolution_digest: Digest | None = None

    @model_validator(mode="after")
    def _command_branches(self) -> "CaseCommandInput":
        if self.operation == "propose_transfer":
            if self.to_owner_id is None:
                raise ValueError("propose_transfer requires to_owner_id")
        elif self.to_owner_id is not None:
            raise ValueError("only propose_transfer may name to_owner_id")
        if self.operation == "resolve":
            if self.expected_resolution_digest is None:
                raise ValueError("resolve requires expected_resolution_digest")
        elif self.expected_resolution_digest is not None:
            raise ValueError(
                "only resolve may name expected_resolution_digest")
        return self


class ReceiptReferences(SourceLocator):
    """Frozen pointer snapshot; no business parameters or arbitrary JSON."""

    source_version: PositiveVersion | None
    source_digest: Digest
    source_state: StateText
    source_verification: SourceVerification
    resolution_kind: ResolutionKind | None = None

    @model_validator(mode="after")
    def _verification_matches_source(self) -> "ReceiptReferences":
        expected = _VERIFICATION_BY_SOURCE[self.source_type]
        if self.source_verification != expected:
            raise ValueError(
                f"{self.source_type} must be verified as {expected!r}")
        return self


class CaseCommandReceipt(_StrictModel):
    """What one command recorded; the raw reason is never exposed."""

    id: Identifier
    case_id: Identifier
    actor_id: Identifier
    operation: Operation
    case_version: PositiveVersion
    request_digest: Digest
    source_digest: Digest
    reason_digest: Digest
    from_owner_id: Identifier | None = None
    to_owner_id: Identifier | None = None
    resolution_digest: Digest | None = None
    references: ReceiptReferences
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _resolution_binding(self) -> "CaseCommandReceipt":
        resolving = self.operation == "resolve"
        if resolving:
            if self.resolution_digest is None:
                raise ValueError("resolve receipts must carry resolution_digest")
            if self.references.resolution_kind is None:
                raise ValueError(
                    "resolve receipts must record a resolution kind")
        elif self.resolution_digest is not None:
            raise ValueError("only resolve receipts may carry resolution_digest")
        elif self.references.resolution_kind is not None:
            raise ValueError("only resolve receipts may record a resolution kind")
        if self.source_digest != self.references.source_digest:
            raise ValueError("source_digest must equal references.source_digest")
        return self


class CaseSummary(SourceLocator):
    """One queue row: ownership state now, not a claim that work is done."""

    id: Identifier
    state: CaseState
    version: PositiveVersion
    initial_source_digest: Digest
    source_version: PositiveVersion | None
    owner_id: Identifier | None = None
    pending_owner_id: Identifier | None = None
    owner_available: bool
    pending_owner_available: bool | None = None
    created_by: Identifier
    created_at: AwareDatetime
    updated_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    resolution_digest: Digest | None = None
    effect: Literal["case_tracking_only"] = EFFECT
    mode: Literal["human_supervised"] = MODE

    @model_validator(mode="after")
    def _ownership_shape(self) -> "CaseSummary":
        # Four states exactly as frozen in B1: a missing owner stays visible, a
        # pending transfer keeps the old owner responsible, and only resolved
        # carries closure time plus proof.
        if self.state == "open":
            if (self.owner_id is not None or self.pending_owner_id is not None
                    or self.resolved_at is not None
                    or self.resolution_digest is not None):
                raise ValueError("an open case has no owner, pending owner or closure")
        elif self.state == "owned":
            if self.owner_id is None:
                raise ValueError("an owned case requires owner_id")
            if (self.pending_owner_id is not None or self.resolved_at is not None
                    or self.resolution_digest is not None):
                raise ValueError("an owned case has no pending owner or closure")
        elif self.state == "transfer_pending":
            if self.owner_id is None or self.pending_owner_id is None:
                raise ValueError(
                    "transfer_pending requires owner_id and pending_owner_id")
            if self.owner_id == self.pending_owner_id:
                raise ValueError("pending owner must differ from current owner")
            if self.resolved_at is not None or self.resolution_digest is not None:
                raise ValueError("a pending transfer carries no closure")
        else:
            if (self.owner_id is None or self.pending_owner_id is not None
                    or self.resolved_at is None
                    or self.resolution_digest is None):
                raise ValueError(
                    "a resolved case requires owner_id, resolved_at and "
                    "resolution_digest")
        if self.owner_id is None and self.owner_available:
            raise ValueError("owner_available must be false without an owner")
        if self.pending_owner_id is None:
            if self.pending_owner_available is not None:
                raise ValueError(
                    "pending_owner_available must be null without a pending owner")
        elif self.pending_owner_available is None:
            raise ValueError(
                "pending_owner_available must be reported for a pending owner")
        return self

    @model_validator(mode="after")
    def _time_order(self) -> "CaseSummary":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.resolved_at is not None and self.resolved_at < self.created_at:
            raise ValueError("resolved_at must not precede created_at")
        return self


class CaseView(CaseSummary):
    """A case plus its freshly re-read source; the source may be unavailable."""

    source: ExceptionSource | None
    blocking_code: BlockingCode | None = None
    source_changed: bool
    command_receipt: CaseCommandReceipt | None = None
    replayed: bool = False

    @model_validator(mode="after")
    def _source_projection(self) -> "CaseView":
        if self.source is None:
            if self.blocking_code is None:
                raise ValueError("a missing source requires blocking_code")
            if self.source_changed:
                raise ValueError("a missing source cannot be reported as changed")
            return self
        if self.blocking_code is not None:
            raise ValueError("blocking_code is only allowed without a source")
        source = self.source
        if (source.source_type, source.source_id, source.kind) != (
                self.source_type, self.source_id, self.kind):
            raise ValueError("source locator must match the case locator")
        if self.source_changed != (
                source.source_digest != self.initial_source_digest):
            raise ValueError(
                "source_changed must be exactly source_digest != "
                "initial_source_digest")
        return self

    @model_validator(mode="after")
    def _receipt_belongs_to_case(self) -> "CaseView":
        if self.command_receipt is None:
            return self
        receipt = self.command_receipt
        if receipt.case_id != self.id:
            raise ValueError("command receipt must belong to this case")
        if receipt.case_version > self.version:
            raise ValueError(
                "command receipt must not lead the current case version")
        return self


class CaseListPage(_CaseConstants):
    items: list[CaseSummary] = Field(max_length=100)
    next_cursor: Cursor | None = None

    @model_validator(mode="after")
    def _page_shape(self) -> "CaseListPage":
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("case ids must be unique within a page")
        if not self.items and self.next_cursor is not None:
            raise ValueError("an empty page cannot carry a cursor")
        return self


class CaseReceiptPage(_CaseConstants):
    case_id: Identifier
    case_version: PositiveVersion
    receipts: list[CaseCommandReceipt] = Field(max_length=100)
    next_after_version: PositiveVersion | None = None

    @model_validator(mode="after")
    def _history_shape(self) -> "CaseReceiptPage":
        versions = [row.case_version for row in self.receipts]
        if versions != sorted(set(versions)):
            raise ValueError(
                "receipt history must be unique and strictly increasing")
        if any(row.case_id != self.case_id for row in self.receipts):
            raise ValueError("receipt history must belong to this case")
        if any(version > self.case_version for version in versions):
            raise ValueError("receipt history must not lead the case version")
        if not self.receipts:
            if self.next_after_version is not None:
                raise ValueError("an empty history cannot carry a cursor")
            return self
        if self.next_after_version is not None:
            if self.next_after_version != versions[-1]:
                raise ValueError(
                    "history cursor must be the last returned case version")
            if self.next_after_version >= self.case_version:
                raise ValueError("history cursor must be older than the case")
        return self


# --------------------------------------------------------------------------- #
# Six-field envelopes (data narrowed to the concrete payload)
# --------------------------------------------------------------------------- #
class _StrictEnvelope(ApiEnvelope):
    """Same six fields as every API response, but strict like the payloads."""

    model_config = ConfigDict(extra="forbid", strict=True,
                              hide_input_in_errors=True)


class ExceptionSourceEnvelope(_StrictEnvelope):
    data: ExceptionSource | None


class CaseEnvelope(_StrictEnvelope):
    data: CaseView | None


class CaseListEnvelope(_StrictEnvelope):
    data: CaseListPage | None


class CaseReceiptPageEnvelope(_StrictEnvelope):
    data: CaseReceiptPage | None


class CaseCommandStatus(_CaseConstants):
    status: Literal["found", "not_found"]
    case: CaseView | None

    @model_validator(mode="after")
    def _confirmed_receipt(self) -> "CaseCommandStatus":
        if self.status == "found":
            if self.case is None or self.case.command_receipt is None:
                raise ValueError("found status requires the exact persisted command receipt")
        elif self.case is not None:
            raise ValueError("not_found carries no case data")
        return self


class CaseCommandStatusEnvelope(_StrictEnvelope):
    data: CaseCommandStatus | None
