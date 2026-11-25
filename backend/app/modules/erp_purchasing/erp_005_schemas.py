"""Strict server-owned receiving snapshots. Synthetic context grants no writes."""
from datetime import datetime
from decimal import Decimal, localcontext
import re
from typing import Annotated, Literal

from pydantic import (AfterValidator, AwareDatetime, BaseModel, BeforeValidator,
                      ConfigDict, Field, StringConstraints, model_validator)
from backend.app.modules.erp_commands.erp_003_wire import canonical_digest
from backend.app.modules.receiving.document_service import compute_line_total

Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=36,
    pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r'^[0-9a-f]{64}$')]
Version = Annotated[int, Field(strict=True, gt=0)]
Money = Annotated[str, StringConstraints(strict=True, min_length=4, max_length=40,
    pattern=r'^(?:0|[1-9][0-9]*)\.[0-9]{2}$')]


def _text(value):
    if value != value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError('receipt_text_invalid')
    return value


Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200), AfterValidator(_text)]
Reason = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=1000), AfterValidator(_text)]
Unit = Literal['piece', 'box', 'kg', 'meter']


def _number(value):
    if Decimal(value) > Decimal('1000000000'):
        raise ValueError('receipt_number_unbounded')
    return value


def _positive(value):
    if Decimal(value) <= 0:
        raise ValueError('receipt_quantity_invalid')
    return value


Number = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=40,
    pattern=r'^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,4})?$'), AfterValidator(_number)]
Quantity = Annotated[Number, AfterValidator(_positive)]
TotalQuantity = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=40,
    pattern=r'^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,4})?$')]


def _time(value):
    if type(value) is datetime:
        return value
    if type(value) is str and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})', value):
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    raise ValueError('receipt_time_invalid')


Timestamp = Annotated[AwareDatetime, BeforeValidator(_time)]


class StrictDTO(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True,
        revalidate_instances='always', hide_input_in_errors=True)


class ReceiptIntentLine(StrictDTO):
    id: Identifier
    product_id: Identifier
    product_version: Version
    quantity: Quantity
    confirmed_by: Identifier
    confirmed_at: Timestamp


class ReceiptIntent(StrictDTO):
    id: Identifier
    version: Version
    status: Literal['awaiting_arrival']
    evidence_id: Identifier
    evidence_sha256: Digest
    supplier_name_claim: Text
    lines: list[ReceiptIntentLine] = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def unique_lines(self):
        if len({x.id for x in self.lines}) != len(self.lines):
            raise ValueError('receipt_intent_duplicate_line')
        return self


class ReceiptLine(StrictDTO):
    id: Identifier
    line_key: Text
    intent_line_id: Identifier
    product_id: Identifier
    product_version: Version
    quantity: Quantity
    unit: Unit
    final_for_intent_line: bool
    evidence_sha256: Digest
    quoted_price: Number | None
    delivered_price: Number | None
    price_evidence_sha256: Digest | None
    line_total: Money | None
    observed_by: Identifier
    observed_at: Timestamp

    @model_validator(mode='after')
    def price_pair(self):
        values = (self.quoted_price, self.delivered_price, self.price_evidence_sha256, self.line_total)
        if any(v is not None for v in values):
            if any(v is None for v in values):
                raise ValueError('receipt_price_incomplete')
            if compute_line_total(self.quantity, self.delivered_price) != Decimal(self.line_total):
                raise ValueError('receipt_line_amount_invalid')
        return self


class ReceiptDecision(StrictDTO):
    id: Identifier
    discrepancy_id: Identifier
    line_id: Identifier
    kind: Literal['shortage', 'overage', 'wrong_item', 'price_change', 'identity_unknown']
    detail_digest: Digest
    batch_version: Version
    decision: Literal['accept_observation']
    reviewer_id: Identifier
    responsible_id: Identifier
    evidence_sha256: Digest
    created_at: Timestamp


class ReceiptBatch(StrictDTO):
    id: Identifier
    version: Version
    intent_id: Identifier
    expected_intent_version: Version
    status: Literal['reviewed']
    delivery_note_sha256: Digest
    created_by: Identifier
    confirmed_by: Identifier
    created_at: Timestamp
    confirmed_at: Timestamp
    lines: list[ReceiptLine] = Field(min_length=1, max_length=100)
    decisions: list[ReceiptDecision] = Field(max_length=500)

    @model_validator(mode='after')
    def reviewed_and_unique(self):
        if self.confirmed_by == self.created_by or self.confirmed_at < self.created_at:
            raise ValueError('receipt_reviewer_invalid')
        if any(len({getattr(x, attr) for x in self.lines}) != len(self.lines) for attr in ('id', 'line_key')):
            raise ValueError('receipt_duplicate_line')
        if len({x.discrepancy_id for x in self.decisions}) != len(self.decisions):
            raise ValueError('receipt_duplicate_decision')
        line_ids = {x.id for x in self.lines}
        for decision in self.decisions:
            if (decision.line_id not in line_ids or decision.batch_version >= self.version
                    or decision.reviewer_id == self.created_by or decision.created_at > self.confirmed_at):
                raise ValueError('receipt_decision_invalid')
        return self


class ReceiptCumulative(StrictDTO):
    intent_line_id: Identifier
    product_id: Identifier
    unit: Unit | None
    expected_quantity: Quantity
    observed_quantity: TotalQuantity
    final_batch_id: Identifier | None


def compute_cumulative(intent, batches):
    """Observed same-unit quantities only; never a stock or conversion claim."""
    intent_lines = {x.id: x for x in intent.lines}
    counts = {x.id: Decimal(0) for x in intent.lines}
    units, finals = {}, {}
    with localcontext() as context:
        context.prec = 80
        for batch in batches:
            if batch.intent_id != intent.id or batch.expected_intent_version != intent.version:
                raise ValueError('receipt_intent_version_changed')
            for line in batch.lines:
                original = intent_lines.get(line.intent_line_id)
                if original is None or (line.product_id, line.product_version) != (original.product_id, original.product_version):
                    raise ValueError('receipt_product_binding_invalid')
                if line.intent_line_id in units and units[line.intent_line_id] != line.unit:
                    raise ValueError('receipt_unknown_conversion')
                units[line.intent_line_id] = line.unit
                counts[line.intent_line_id] += Decimal(line.quantity)
                if line.final_for_intent_line:
                    if line.intent_line_id in finals:
                        raise ValueError('receipt_duplicate_final_batch')
                    finals[line.intent_line_id] = batch.id
        by_id = {b.id: b for b in batches}
        for batch in batches:
            for line in batch.lines:
                final = finals.get(line.intent_line_id)
                if final and batch.created_at > by_id[final].created_at:
                    raise ValueError('receipt_after_final_batch')
        return [ReceiptCumulative(intent_line_id=x.id, product_id=x.product_id,
            unit=units.get(x.id), expected_quantity=x.quantity,
            observed_quantity=format(counts[x.id], 'f'), final_batch_id=finals.get(x.id)) for x in intent.lines]


class ReceiptSourceSnapshot(StrictDTO):
    organization_id: Identifier
    store_id: Identifier
    batch_id: Identifier
    batch_version: Version
    intent: ReceiptIntent
    related_batches: list[ReceiptBatch] = Field(min_length=1, max_length=100)
    cumulative: list[ReceiptCumulative] = Field(min_length=1, max_length=100)
    currency: Literal['CNY']
    currency_source: Literal['synthetic_contract_only']
    total_amount: Money
    effect: Literal['local_sandbox_only']

    @model_validator(mode='after')
    def complete_snapshot(self):
        if (len({b.id for b in self.related_batches}) != len(self.related_batches)
                or len({b.delivery_note_sha256 for b in self.related_batches}) != len(self.related_batches)):
            raise ValueError('receipt_duplicate_batch_or_note')
        current = [b for b in self.related_batches if b.id == self.batch_id]
        if len(current) != 1 or current[0].version != self.batch_version:
            raise ValueError('receipt_current_batch_invalid')
        batch = current[0]
        with localcontext() as context:
            context.prec = 80
            if any(x.line_total is None for x in batch.lines) or sum(
                    (Decimal(x.line_total) for x in batch.lines), Decimal(0)) != Decimal(self.total_amount):
                raise ValueError('receipt_current_amount_incomplete')
        if self.cumulative != compute_cumulative(self.intent, self.related_batches):
            raise ValueError('receipt_cumulative_invalid')
        # Recompute exception approval from the WHOLE frozen batch set. An
        # old per-batch check (or a concurrent import) is not proof that the
        # current overage/final shortage has been independently accepted.
        for count in self.cumulative:
            expected, observed = Decimal(count.expected_quantity), Decimal(count.observed_quantity)
            kind = 'overage' if observed > expected else (
                'shortage' if count.final_batch_id is not None and observed < expected else None)
            if kind is None:
                continue
            evidence = canonical_digest(dict(expected_quantity=format(expected, 'f'),
                cumulative_observed_quantity=format(observed, 'f'),
                unit_verification='employee_attested_unknown_conversion'))
            matches = []
            for related in self.related_batches:
                ids = {line.id for line in related.lines if line.intent_line_id == count.intent_line_id}
                matches.extend(d for d in related.decisions if d.line_id in ids
                    and d.kind == kind and d.detail_digest == evidence)
            if not matches:
                raise ValueError('receipt_quantity_decision_missing')
        return self

    @property
    def batch(self):
        return next(b for b in self.related_batches if b.id == self.batch_id)


class ReceiptSourceContext(StrictDTO):
    snapshot: ReceiptSourceSnapshot
    source_digest: Digest

    @model_validator(mode='after')
    def source_binding(self):
        if canonical_digest(self.snapshot.model_dump(mode='json')) != self.source_digest:
            raise ValueError('receipt_source_digest_invalid')
        return self


class ReceiptProposeInput(StrictDTO):
    batch_id: Identifier
    expected_batch_version: Version
    expected_source_digest: Digest
    expires_at: Timestamp
    reason: Reason


class ReceiptDecideInput(StrictDTO):
    request_digest: Digest
    decision: Literal['approved', 'rejected']
    expires_at: Timestamp
    reason: Reason


class ReceiptStageInput(StrictDTO):
    request_digest: Digest
    approval_id: Identifier
    expected_batch_version: Version


# Response DTOs intentionally live below the frozen source/input contracts.
# They expose only the public ERP-005 view, never the account/session/fence or
# idempotency fields held by a dispatch attempt.
import base64
import binascii

from backend.app.core.errors import ApiEnvelope
from backend.app.modules.erp_commands.erp_002_schemas import (
    ERPApprovalReceipt, ERPCommandOut, ERPRequestDetails,
)


class ReceiptERPRequestDetails(ERPRequestDetails):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True,
        revalidate_instances='always', hide_input_in_errors=True)

    action: Literal['goods_receipt_document_proposal']
    target_id: Identifier
    target_version: Version
    requested_by: Identifier
    source_sha256: Digest
    business_parameters: dict[str, str]


class ReceiptERPApprovalReceipt(ERPApprovalReceipt):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True,
        revalidate_instances='always', hide_input_in_errors=True)

    id: Identifier
    approved_by: Identifier
    reason: Reason
    request_digest: Digest
    expires_at: Timestamp


class ReceiptERPCommandOut(ERPCommandOut):
    """Strict ERP-002 command receipt for a local synthetic goods receipt."""
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True,
        revalidate_instances='always', hide_input_in_errors=True)

    id: Identifier
    state: Literal['pending', 'approved', 'rejected', 'staged', 'unknown', 'manual_rejected']
    request_digest: Digest
    target_digest: Digest
    expires_at: Timestamp
    replayed: bool = Field(strict=True, default=False)
    request: ReceiptERPRequestDetails
    approval: ReceiptERPApprovalReceipt | None


ErrorCode = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64,
    pattern=r'^[A-Z][A-Z0-9_]*$')]


class ReceiptLineResult(StrictDTO):
    line_id: Identifier
    status: Literal['pending', 'accepted', 'rejected', 'unknown']
    external_line_ref: Text | None
    error_code: ErrorCode | None

    @model_validator(mode='after')
    def evidence_matches_status(self):
        if self.status == 'accepted':
            if self.external_line_ref is None or self.error_code is not None:
                raise ValueError('receipt_line_result_invalid')
        elif self.status == 'rejected':
            if self.external_line_ref is not None or self.error_code is None:
                raise ValueError('receipt_line_result_invalid')
        elif self.external_line_ref is not None:
            raise ValueError('receipt_line_result_invalid')
        elif self.status == 'pending' and self.error_code is not None:
            raise ValueError('receipt_line_result_invalid')
        return self


class ReceiptAttemptView(StrictDTO):
    id: Identifier
    intent_id: Identifier
    executor_id: Identifier
    state: Literal['prepared', 'dispatched', 'unknown', 'partial', 'sandbox_verified', 'failed']
    version: Version
    input_digest: Digest
    line_results: list[ReceiptLineResult] = Field(min_length=1, max_length=100)
    delivery_ref: Text | None
    external_ref: Text | None
    receipt_digest: Digest | None
    created_at: Timestamp
    updated_at: Timestamp
    dispatched_at: Timestamp | None

    @model_validator(mode='after')
    def consistent_attempt(self):
        if len({line.line_id for line in self.line_results}) != len(self.line_results):
            raise ValueError('receipt_attempt_duplicate_line')
        if self.updated_at < self.created_at:
            raise ValueError('receipt_attempt_time_invalid')
        if self.dispatched_at is not None and self.dispatched_at < self.created_at:
            raise ValueError('receipt_attempt_time_invalid')
        if self.dispatched_at is not None and self.updated_at < self.dispatched_at:
            raise ValueError('receipt_attempt_time_invalid')
        if self.state == 'prepared' and self.dispatched_at is not None:
            raise ValueError('receipt_attempt_state_invalid')
        if self.state != 'prepared' and self.dispatched_at is None:
            raise ValueError('receipt_attempt_state_invalid')
        if self.state == 'sandbox_verified' and (
                any(line.status != 'accepted' for line in self.line_results)
                or self.external_ref is None or self.receipt_digest is None):
            raise ValueError('receipt_attempt_receipt_invalid')
        return self


class ReceiptCommandView(StrictDTO):
    id: Identifier
    batch_id: Identifier
    purchase_intent_id: Identifier
    delivery_note_sha256: Digest
    route: Literal['api']
    proposal_digest: Digest
    created_at: Timestamp
    source_version: Version
    source_digest: Digest
    source_snapshot: ReceiptSourceSnapshot
    request: ReceiptERPCommandOut
    state: Literal[
        'prepared', 'dispatched', 'unknown', 'partial', 'sandbox_verified', 'failed',
        'pending', 'approved', 'rejected', 'staged', 'manual_rejected',
    ]
    attempt: ReceiptAttemptView | None
    mode: Literal['synthetic']
    effect: Literal['local_sandbox_only']
    inventory_state: Literal['not_posted']
    purchase_state: Literal['not_verified']
    replayed: bool = Field(strict=True)

    @model_validator(mode='after')
    def bindings_match(self):
        snapshot = self.source_snapshot
        if (
            snapshot.batch_id != self.batch_id
            or snapshot.batch_version != self.source_version
            or snapshot.intent.id != self.purchase_intent_id
            or snapshot.batch.delivery_note_sha256 != self.delivery_note_sha256
            or canonical_digest(snapshot.model_dump(mode='json')) != self.source_digest
        ):
            raise ValueError('receipt_command_source_binding_invalid')

        request = self.request
        details = request.request
        parameters = details.business_parameters
        if (
            details.action != 'goods_receipt_document_proposal'
            or details.target_id != self.batch_id
            or details.target_version != self.source_version
            or details.source_sha256 != self.source_digest
            or request.target_digest != self.source_digest
            or parameters != {'route': self.route, 'source_digest': self.source_digest}
        ):
            raise ValueError('receipt_command_request_binding_invalid')

        if self.attempt is not None:
            source_line_ids = {line.id for line in snapshot.batch.lines}
            attempt_line_ids = {line.line_id for line in self.attempt.line_results}
            if attempt_line_ids != source_line_ids or self.state != self.attempt.state:
                raise ValueError('receipt_command_attempt_binding_invalid')
        elif self.state != request.state:
            raise ValueError('receipt_command_state_binding_invalid')
        return self


class ReceiptCommandStatus(StrictDTO):
    status: Literal['found', 'not_found']
    command: ReceiptCommandView | None

    @model_validator(mode='after')
    def found_matches_command(self):
        if (self.status == 'found') != (self.command is not None):
            raise ValueError('receipt_command_status_invalid')
        return self


class ReceiptObservationView(StrictDTO):
    id: Identifier
    attempt_id: Identifier
    actor_id: Identifier
    kind: Literal['dispatch_intent', 'ack', 'readback', 'network_error']
    evidence_digest: Digest
    created_at: Timestamp


Cursor = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256,
    pattern=r'^[A-Za-z0-9_-]+={0,2}$')]


def _receipt_cursor(value):
    if value != value.strip():
        raise ValueError('receipt_cursor_invalid')
    unpadded = value.rstrip('=')
    padding = len(value) - len(unpadded)
    if not unpadded or len(unpadded) % 4 == 1:
        raise ValueError('receipt_cursor_invalid')
    if padding and (len(value) % 4 != 0 or padding != (-len(unpadded)) % 4):
        raise ValueError('receipt_cursor_invalid')
    try:
        raw = base64.b64decode(value + '=' * ((-len(value)) % 4), altchars=b'-_', validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('receipt_cursor_invalid') from None
    if base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=') != unpadded:
        raise ValueError('receipt_cursor_invalid')
    return value


StrictCursor = Annotated[Cursor, AfterValidator(_receipt_cursor)]


class ReceiptObservationPage(StrictDTO):
    command_id: Identifier
    attempt_id: Identifier | None
    items: list[ReceiptObservationView] = Field(max_length=100)
    next_cursor: StrictCursor | None

    @model_validator(mode='after')
    def page_matches_attempt(self):
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError('receipt_observation_duplicate_id')
        if self.items and self.attempt_id is None:
            raise ValueError('receipt_observation_attempt_required')
        if any(item.attempt_id != self.attempt_id for item in self.items):
            raise ValueError('receipt_observation_attempt_mismatch')
        if self.attempt_id is None and self.next_cursor is not None:
            raise ValueError('receipt_observation_cursor_without_attempt')
        return self


class StrictApiEnvelope(ApiEnvelope):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True,
        revalidate_instances='always', hide_input_in_errors=True)

    success: bool = Field(strict=True)
    data: object | None
    code: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64,
        pattern=r'^[A-Z][A-Z0-9_]*$')]
    error: Annotated[str, StringConstraints(strict=True, max_length=1000)] | None
    user_message: Text
    hint: Text | None

    @model_validator(mode='after')
    def success_shape_matches(self):
        if self.success:
            if self.code != 'OK' or self.error is not None or self.data is None:
                raise ValueError('receipt_envelope_success_invalid')
        elif self.data is not None or not self.error or self.code == 'OK':
            raise ValueError('receipt_envelope_failure_invalid')
        return self


class ReceiptSourceEnvelope(StrictApiEnvelope):
    data: ReceiptSourceContext | None


class ReceiptCommandEnvelope(StrictApiEnvelope):
    data: ReceiptCommandView | None


class ReceiptCommandStatusEnvelope(StrictApiEnvelope):
    data: ReceiptCommandStatus | None


class ReceiptObservationPageEnvelope(StrictApiEnvelope):
    data: ReceiptObservationPage | None
