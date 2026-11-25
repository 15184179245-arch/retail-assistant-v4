"""ERP-004 strict sales snapshots and commands; no client price/authority writes."""
import base64
import binascii
from datetime import datetime
from decimal import Decimal, localcontext, ROUND_HALF_UP
import re
from typing import Annotated, Literal

from pydantic import (
    AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints,
    BeforeValidator, model_validator,
)
from backend.app.core.errors import ApiEnvelope
from backend.app.modules.erp_commands.erp_002_schemas import (
    ERPApprovalReceipt,
    ERPCommandOut,
    ERPRequestDetails,
)
from backend.app.modules.erp_commands.erp_003_wire import canonical_digest

Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=36,
    pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r'^[0-9a-f]{64}$')]
Version = Annotated[int, Field(strict=True, gt=0)]
Currency = Annotated[str, StringConstraints(strict=True, pattern=r'^[A-Z]{3}$')]
Route = Literal['api','pos']


def _text(value):
    if value != value.strip() or any(ord(c)<32 for c in value):
        raise ValueError('sales_text_invalid')
    return value


Text200 = Annotated[str, StringConstraints(strict=True,min_length=1,max_length=200),AfterValidator(_text)]
Reason = Annotated[str, StringConstraints(strict=True,min_length=1,max_length=1000),AfterValidator(_text)]
Money = Annotated[str, StringConstraints(strict=True,min_length=4,max_length=40,pattern=r'^(?:0|[1-9][0-9]*)\.[0-9]{2}$')]


def _quantity(value):
    if Decimal(value)<=0:
        raise ValueError('sales_quantity_invalid')
    return value


Quantity = Annotated[str, StringConstraints(strict=True,min_length=1,max_length=40,
    pattern=r'^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$'),AfterValidator(_quantity)]


def _time(value):
    if type(value) is datetime:
        return value
    if type(value) is str and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})',value):
        return datetime.fromisoformat(value.replace('Z','+00:00'))
    raise ValueError('sales_timestamp_invalid')


Timestamp = Annotated[AwareDatetime,BeforeValidator(_time)]


class StrictDTO(BaseModel):
    model_config=ConfigDict(strict=True,extra='forbid',frozen=True,revalidate_instances='always',hide_input_in_errors=True)


def compute_line_amounts(unit_price, quantity, discount_bps):
    """Same two-step rounding as sales/pricing; derived values, no writes."""
    with localcontext() as context:
        context.prec=80
        unit=(Decimal(unit_price)*Decimal(10000-discount_bps)/Decimal(10000)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
        amount=(unit*Decimal(quantity)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
    return unit,amount


class SalesSourceLine(StrictDTO):
    id: Identifier
    line_key: Text200
    product_id: Text200
    product_name: Text200
    quote_id: Identifier
    quantity: Quantity
    unit_price: Money
    discount_bps: Annotated[int,Field(strict=True,ge=0,le=10000)]
    discounted_unit_price: Money
    line_amount: Money
    confirmed_by: Identifier
    confirmed_at: Timestamp

    @model_validator(mode='after')
    def consistent_amount(self):
        unit,amount=compute_line_amounts(self.unit_price,self.quantity,self.discount_bps)
        if unit!=Decimal(self.discounted_unit_price) or amount!=Decimal(self.line_amount):
            raise ValueError('sales_line_amount_inconsistent')
        return self


class SalesSourceSnapshot(StrictDTO):
    organization_id: Identifier
    store_id: Identifier
    draft_id: Identifier
    draft_version: Version
    handoff_id: Identifier | None
    stage: Literal['ready']
    status: Literal['pending']
    currency: Currency
    total_amount: Money
    expires_at: Timestamp | None
    lines: list[SalesSourceLine] = Field(min_length=1,max_length=50)

    @model_validator(mode='after')
    def consistent_lines_total(self):
        for attribute in ('id','line_key'):
            if len({getattr(line,attribute) for line in self.lines})!=len(self.lines):
                raise ValueError('sales_duplicate_line')
        with localcontext() as context:
            context.prec=80
            total=sum((Decimal(line.line_amount) for line in self.lines),Decimal(0))
        if total!=Decimal(self.total_amount):
            raise ValueError('sales_total_inconsistent')
        return self


class SalesSourceContext(StrictDTO):
    snapshot: SalesSourceSnapshot
    source_digest: Digest

    @model_validator(mode='after')
    def digest_matches(self):
        if canonical_digest(self.snapshot.model_dump(mode='json'))!=self.source_digest:
            raise ValueError('sales_source_digest_invalid')
        return self


class SalesProposeInput(StrictDTO):
    draft_id: Identifier
    expected_draft_version: Version
    expected_source_digest: Digest
    route: Route
    expires_at: Timestamp
    reason: Reason


class SalesDecideInput(StrictDTO):
    request_digest: Digest
    decision: Literal['approved','rejected']
    reason: Reason
    expires_at: Timestamp


class SalesStageInput(StrictDTO):
    request_digest: Digest
    approval_id: Identifier
    expected_draft_version: Version


ErrorCode = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64,
    pattern=r'^[A-Z][A-Z0-9_]*$')]
Cursor = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256,
    pattern=r'^[A-Za-z0-9_-]+={0,2}$')]


def _cursor(value):
    """Accept canonical base64url cursors, with or without terminal padding."""
    if value != value.strip():
        raise ValueError('sales_cursor_invalid')
    unpadded = value.rstrip('=')
    padding = len(value) - len(unpadded)
    if not unpadded or len(unpadded) % 4 == 1:
        raise ValueError('sales_cursor_invalid')
    if padding and (len(value) % 4 != 0 or padding != (-len(unpadded)) % 4):
        raise ValueError('sales_cursor_invalid')
    try:
        base64.b64decode(value + '=' * ((-len(value)) % 4), altchars=b'-_', validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('sales_cursor_invalid') from None
    return value


StrictCursor = Annotated[Cursor, AfterValidator(_cursor)]


class SalesLineResult(StrictDTO):
    line_id: Identifier
    status: Literal['pending', 'accepted', 'rejected', 'unknown']
    external_line_ref: Text200 | None
    error_code: ErrorCode | None

    @model_validator(mode='after')
    def evidence_matches_status(self):
        if self.status == 'accepted':
            if self.external_line_ref is None or self.error_code is not None:
                raise ValueError('sales_line_result_invalid')
        elif self.status == 'rejected':
            if self.external_line_ref is not None or self.error_code is None:
                raise ValueError('sales_line_result_invalid')
        elif self.external_line_ref is not None:
            raise ValueError('sales_line_result_invalid')
        elif self.status == 'pending' and self.error_code is not None:
            raise ValueError('sales_line_result_invalid')
        return self


class SalesAttemptView(StrictDTO):
    id: Identifier
    intent_id: Identifier
    executor_id: Identifier
    state: Literal['prepared', 'dispatched', 'unknown', 'partial', 'sandbox_verified', 'failed']
    version: Version
    input_digest: Digest
    line_results: list[SalesLineResult] = Field(min_length=1, max_length=50)
    delivery_ref: Text200 | None
    external_ref: Text200 | None
    receipt_digest: Digest | None
    created_at: Timestamp
    updated_at: Timestamp
    dispatched_at: Timestamp | None

    @model_validator(mode='after')
    def consistent_attempt(self):
        if len({line.line_id for line in self.line_results}) != len(self.line_results):
            raise ValueError('sales_attempt_duplicate_line')
        if self.updated_at < self.created_at:
            raise ValueError('sales_attempt_time_invalid')
        if self.dispatched_at is not None and self.dispatched_at < self.created_at:
            raise ValueError('sales_attempt_time_invalid')
        if self.state == 'prepared' and self.dispatched_at is not None:
            raise ValueError('sales_attempt_state_invalid')
        if self.state != 'prepared' and self.dispatched_at is None:
            raise ValueError('sales_attempt_state_invalid')
        if self.state == 'sandbox_verified' and (
            any(line.status != 'accepted' for line in self.line_results)
            or self.external_ref is None
            or self.receipt_digest is None
        ):
            raise ValueError('sales_attempt_receipt_invalid')
        return self


class SalesERPRequestDetails(ERPRequestDetails):
    """Frozen ERP-002 response shape with an existing ERP-004 handoff binding."""
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True,
        revalidate_instances='always', hide_input_in_errors=True)

    action: Literal['sales_document_proposal']
    target_id: Identifier
    target_version: Version
    requested_by: Identifier
    source_sha256: Digest
    business_parameters: dict[str, str]


class SalesERPApprovalReceipt(ERPApprovalReceipt):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True,
        revalidate_instances='always', hide_input_in_errors=True)

    id: Identifier
    approved_by: Identifier
    reason: Reason
    request_digest: Digest
    expires_at: Timestamp


class SalesERPCommandOut(ERPCommandOut):
    """Strict, immutable ERP-002 receipt used as an ERP-004 view component."""
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True,
        revalidate_instances='always', hide_input_in_errors=True)

    id: Identifier
    state: Literal['pending', 'approved', 'rejected', 'staged', 'unknown', 'manual_rejected']
    request_digest: Digest
    target_digest: Digest
    expires_at: Timestamp
    replayed: bool = False
    request: SalesERPRequestDetails
    approval: SalesERPApprovalReceipt | None


class SalesCommandView(StrictDTO):
    id: Identifier
    draft_id: Identifier
    handoff_id: Identifier
    route: Route
    source_version: Version
    source_digest: Digest
    source_snapshot: SalesSourceSnapshot
    proposal_digest: Digest
    created_at: Timestamp
    request: SalesERPCommandOut
    state: Literal['pending', 'approved', 'rejected', 'staged', 'manual_rejected',
                    'prepared', 'dispatched', 'unknown', 'partial', 'sandbox_verified', 'failed']
    attempt: SalesAttemptView | None
    mode: Literal['synthetic']
    effect: Literal['local_sandbox_only']
    payment_state: Literal['not_verified']
    replayed: bool

    @model_validator(mode='after')
    def bindings_match(self):
        snapshot = self.source_snapshot
        request = self.request
        request_details = request.request
        parameters = request_details.business_parameters
        if (
            snapshot.draft_id != self.draft_id
            or snapshot.draft_version != self.source_version
            or snapshot.handoff_id != self.handoff_id
            or canonical_digest(snapshot.model_dump(mode='json')) != self.source_digest
        ):
            raise ValueError('sales_command_source_binding_invalid')
        if (
            request_details.action != 'sales_document_proposal'
            or request_details.target_id != self.draft_id
            or request_details.target_version != self.source_version
            or request.target_digest != self.source_digest
            or request_details.source_sha256 != self.source_digest
            or set(parameters) != {'route', 'source_digest', 'handoff_id'}
            or parameters.get('route') != self.route
            or parameters.get('source_digest') != self.source_digest
            or parameters.get('handoff_id') != self.handoff_id
        ):
            raise ValueError('sales_command_request_binding_invalid')
        if self.attempt is not None:
            snapshot_line_ids = {line.id for line in snapshot.lines}
            attempt_line_ids = {line.line_id for line in self.attempt.line_results}
            if attempt_line_ids != snapshot_line_ids or self.state != self.attempt.state:
                raise ValueError('sales_command_attempt_binding_invalid')
        elif self.state != request.state:
            raise ValueError('sales_command_state_binding_invalid')
        return self


class SalesCommandStatus(StrictDTO):
    status: Literal['found', 'not_found']
    command: SalesCommandView | None

    @model_validator(mode='after')
    def found_matches_command(self):
        if (self.status == 'found') != (self.command is not None):
            raise ValueError('sales_command_status_invalid')
        return self


class SalesObservationView(StrictDTO):
    id: Identifier
    attempt_id: Identifier
    actor_id: Identifier
    kind: Literal['dispatch_intent', 'ack', 'readback', 'network_error']
    evidence_digest: Digest
    created_at: Timestamp


class SalesObservationPage(StrictDTO):
    command_id: Identifier
    attempt_id: Identifier | None
    items: list[SalesObservationView] = Field(max_length=100)
    next_cursor: StrictCursor | None

    @model_validator(mode='after')
    def page_matches_attempt(self):
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError('sales_observation_duplicate_id')
        if self.items and self.attempt_id is None:
            raise ValueError('sales_observation_attempt_required')
        if any(item.attempt_id != self.attempt_id for item in self.items):
            raise ValueError('sales_observation_attempt_mismatch')
        return self


class StrictApiEnvelope(ApiEnvelope):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True,
        revalidate_instances='always', hide_input_in_errors=True)

    success: bool
    data: object | None
    code: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]
    error: str | None
    user_message: str
    hint: str | None

    @model_validator(mode='after')
    def success_shape_matches(self):
        if self.success:
            if self.code != 'OK' or self.error is not None or self.data is None:
                raise ValueError('sales_envelope_success_invalid')
        elif self.data is not None or not self.error or self.code == 'OK':
            raise ValueError('sales_envelope_failure_invalid')
        return self


class SalesSourceEnvelope(StrictApiEnvelope):
    data: SalesSourceContext | None


class SalesCommandEnvelope(StrictApiEnvelope):
    data: SalesCommandView | None


class SalesCommandStatusEnvelope(StrictApiEnvelope):
    data: SalesCommandStatus | None


class SalesObservationPageEnvelope(StrictApiEnvelope):
    data: SalesObservationPage | None
