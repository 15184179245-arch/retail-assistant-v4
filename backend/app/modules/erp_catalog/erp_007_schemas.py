"""ERP-007 explicit field selection and independent synthetic catalog context."""
from datetime import datetime
import re
from typing import Annotated, Literal, Union

from pydantic import (AwareDatetime, BaseModel, BeforeValidator, ConfigDict,
    Field, StringConstraints, model_validator)
from backend.app.modules.erp_commands.erp_003_wire import canonical_digest

Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=36,
    pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r'^[0-9a-f]{64}$')]
Version = Annotated[int, Field(strict=True, gt=0)]
RawValue = Annotated[str, StringConstraints(strict=True, max_length=2000)]
Text200 = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
Reason = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=1000)]
FieldName = Literal['display_label', 'specification_note']
Operation = Literal['text_update', 'image_upload', 'image_bind']


def _time(value):
    if type(value) is datetime:
        return value
    if type(value) is str and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})', value):
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    raise ValueError('catalog_timestamp_invalid')


Timestamp = Annotated[AwareDatetime, BeforeValidator(_time)]


class StrictDTO(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True,
        revalidate_instances='always', hide_input_in_errors=True)


class TextFields(StrictDTO):
    display_label: RawValue | None
    specification_note: RawValue | None


class TextUpdateInput(StrictDTO):
    operation: Literal['text_update']
    field_name: FieldName
    new_value: RawValue

    @model_validator(mode='after')
    def selected_value(self):
        if not self.new_value or self.new_value != self.new_value.strip():
            raise ValueError('catalog_new_value_invalid')
        maximum = 200 if self.field_name == 'display_label' else 2000
        if len(self.new_value) > maximum or any(ord(c) < 32 and (
                self.field_name == 'display_label' or c not in '\n\t') for c in self.new_value):
            raise ValueError('catalog_new_value_invalid')
        return self


class ImageUploadInput(StrictDTO):
    operation: Literal['image_upload']
    asset_id: Identifier


class ImageBindInput(StrictDTO):
    operation: Literal['image_bind']
    asset_id: Identifier
    upload_command_id: Identifier


ActionSelection = Annotated[Union[TextUpdateInput, ImageUploadInput, ImageBindInput], Field(discriminator='operation')]


class CatalogSource(StrictDTO):
    organization_id: Identifier
    store_id: Identifier
    product_id: Identifier
    product_version: Version
    source_system: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=40)]
    external_code: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=120)]
    source_version: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=80)]
    source_updated_at: Timestamp | None
    snapshot_id: Identifier
    snapshot_sha256: Digest
    snapshot_source_updated_at: Timestamp | None
    snapshot_captured_at: Timestamp
    snapshot_payload_digest: Digest
    original_fields: TextFields
    extension_id: Identifier | None
    extension_version: Version | None
    extension_digest: Digest | None
    target_version: Version
    sandbox_remote_version: Version
    sandbox_fields: TextFields
    sandbox_image_ref: Text200 | None
    real_erp_mapping: Literal['unverified']
    effect: Literal['local_sandbox_only']

    @model_validator(mode='after')
    def extension_binding(self):
        values = (self.extension_id, self.extension_version, self.extension_digest)
        if any(v is not None for v in values) and any(v is None for v in values):
            raise ValueError('catalog_extension_binding_invalid')
        return self


class AssetSnapshot(StrictDTO):
    id: Identifier
    category: Literal['category_image', 'inventory_photo']
    content_type: Literal['image/jpeg', 'image/png', 'image/webp']
    byte_size: Annotated[int, Field(strict=True, ge=1, le=5242880)]
    sha256: Digest
    created_by: Identifier
    created_at: Timestamp


class UploadProof(StrictDTO):
    command_id: Identifier
    attempt_id: Identifier
    product_id: Identifier
    asset_id: Identifier
    asset_sha256: Digest
    byte_size: Annotated[int, Field(strict=True, ge=1, le=5242880)]
    content_type: Literal['image/jpeg', 'image/png', 'image/webp']
    input_digest: Digest
    receipt_digest: Digest
    asset_ref: Text200
    verification_state: Literal['sandbox_verified']


class TextAction(TextUpdateInput):
    original_value: RawValue | None
    current_value: RawValue | None

    @model_validator(mode='after')
    def real_difference(self):
        if self.current_value == self.new_value:
            raise ValueError('catalog_no_change')
        return self


class UploadAction(StrictDTO):
    operation: Literal['image_upload']
    asset: AssetSnapshot


class BindAction(StrictDTO):
    operation: Literal['image_bind']
    asset: AssetSnapshot
    upload_proof: UploadProof


ActionSnapshot = Annotated[Union[TextAction, UploadAction, BindAction], Field(discriminator='operation')]


class CatalogContext(StrictDTO):
    source: CatalogSource
    action: ActionSnapshot

    @model_validator(mode='after')
    def source_action_binding(self):
        if self.action.operation == 'text_update':
            name = self.action.field_name
            if (self.action.original_value != getattr(self.source.original_fields, name)
                    or self.action.current_value != getattr(self.source.sandbox_fields, name)):
                raise ValueError('catalog_text_binding_invalid')
        elif self.action.operation == 'image_bind':
            asset, proof = self.action.asset, self.action.upload_proof
            if (proof.product_id != self.source.product_id or proof.asset_id != asset.id
                    or proof.asset_sha256 != asset.sha256 or proof.content_type != asset.content_type
                    or proof.byte_size != asset.byte_size):
                raise ValueError('catalog_upload_proof_binding_invalid')
            if proof.asset_ref == self.source.sandbox_image_ref:
                raise ValueError('catalog_no_change')
        return self


class CatalogContextView(StrictDTO):
    context: CatalogContext
    source_digest: Digest
    action_digest: Digest
    context_digest: Digest

    @model_validator(mode='after')
    def context_binding(self):
        if (canonical_digest(self.context.source.model_dump(mode='json')) != self.source_digest
                or canonical_digest(self.context.action.model_dump(mode='json')) != self.action_digest
                or canonical_digest(self.context.model_dump(mode='json')) != self.context_digest):
            raise ValueError('catalog_context_digest_invalid')
        return self


class CatalogPreviewInput(StrictDTO):
    product_id: Identifier
    action: ActionSelection


class CatalogProposeInput(CatalogPreviewInput):
    expected_target_version: Version
    expected_context_digest: Digest
    expires_at: Timestamp
    reason: Reason


class CatalogDecideInput(StrictDTO):
    request_digest: Digest
    decision: Literal['approved', 'rejected']
    expires_at: Timestamp
    reason: Reason


class CatalogStageInput(StrictDTO):
    request_digest: Digest
    approval_id: Identifier
    expected_target_version: Version


ErrorCode = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64,
    pattern=r'^[A-Z][A-Z0-9_]*$')]


class CatalogResult(StrictDTO):
    """Normalized signed peer fact; never a source or permission replacement."""
    operation: Operation
    outcome: Literal['accepted', 'rejected']
    remote_version: Version
    action_digest: Digest
    field_name: FieldName | None
    field_value: RawValue | None
    asset_ref: Text200 | None
    asset_sha256: Digest | None
    error_code: ErrorCode | None

    @model_validator(mode='after')
    def result_shape(self):
        if self.outcome == 'rejected':
            if (not self.error_code or any(x is not None for x in (
                    self.field_name, self.field_value, self.asset_ref, self.asset_sha256))):
                raise ValueError('catalog_rejection_invalid')
        elif self.error_code is not None:
            raise ValueError('catalog_acceptance_invalid')
        elif self.operation == 'text_update':
            if self.field_name is None or self.field_value is None or self.asset_ref is not None or self.asset_sha256 is not None:
                raise ValueError('catalog_text_result_invalid')
        elif self.asset_ref is None or self.asset_sha256 is None or self.field_name is not None or self.field_value is not None:
            raise ValueError('catalog_image_result_invalid')
        return self


# Public read models intentionally expose only the fields required to review
# one synthetic maintenance action. Account/session credentials and image
# bytes remain outside the response contract.
import base64
import binascii
from pydantic import AfterValidator


class CatalogERPParameters(StrictDTO):
    operation: Operation
    action_digest: Digest
    context_digest: Digest


class CatalogERPRequestDetails(StrictDTO):
    action: Literal['catalog_maintenance_proposal']
    target_id: Identifier
    target_version: Version
    requested_by: Identifier
    source_sha256: Digest
    business_parameters: CatalogERPParameters


class CatalogERPApprovalReceipt(StrictDTO):
    id: Identifier
    approved_by: Identifier
    decision: Literal['approved', 'rejected']
    reason: Reason
    request_digest: Digest
    expires_at: Timestamp


class CatalogERPCommandOut(StrictDTO):
    """The linked ERP-002 approval request, excluding formal outcomes."""

    id: Identifier
    state: Literal['pending', 'approved', 'rejected', 'staged', 'unknown', 'manual_rejected']
    request_digest: Digest
    target_digest: Digest
    expires_at: Timestamp
    replayed: bool = Field(strict=True)
    request: CatalogERPRequestDetails
    approval: CatalogERPApprovalReceipt | None


class CatalogAttemptView(StrictDTO):
    id: Identifier
    intent_id: Identifier
    executor_id: Identifier
    state: Literal['prepared', 'dispatched', 'unknown', 'sandbox_verified', 'failed', 'cancelled']
    version: Version
    input_digest: Digest
    proof: CatalogResult | None
    result_remote_version: Version | None
    applied_at: Timestamp | None
    delivery_ref: Text200 | None
    external_ref: Text200 | None
    receipt_digest: Digest | None
    created_at: Timestamp
    updated_at: Timestamp
    dispatched_at: Timestamp | None

    @model_validator(mode='after')
    def consistent_attempt(self):
        if self.updated_at < self.created_at:
            raise ValueError('catalog_attempt_time_invalid')
        if self.dispatched_at is not None and (
                self.dispatched_at < self.created_at or self.updated_at < self.dispatched_at):
            raise ValueError('catalog_attempt_time_invalid')
        if self.applied_at is not None and (
                self.dispatched_at is None or self.applied_at < self.dispatched_at
                or self.updated_at < self.applied_at):
            raise ValueError('catalog_attempt_time_invalid')
        if (self.state in ('prepared', 'cancelled')) != (self.dispatched_at is None):
            raise ValueError('catalog_attempt_state_invalid')
        terminal = self.state in ('sandbox_verified', 'failed')
        if terminal != (self.proof is not None and self.receipt_digest is not None
                        and self.result_remote_version is not None and self.applied_at is not None):
            raise ValueError('catalog_attempt_proof_invalid')
        if not terminal and any(v is not None for v in (self.proof, self.receipt_digest, self.result_remote_version, self.applied_at)):
            raise ValueError('catalog_attempt_proof_invalid')
        if self.proof is None:
            if self.receipt_digest is not None or self.result_remote_version is not None or self.external_ref is not None:
                raise ValueError('catalog_attempt_proof_invalid')
        elif self.proof.remote_version != self.result_remote_version or self.proof.asset_ref != self.external_ref:
            raise ValueError('catalog_attempt_proof_invalid')
        if self.state == 'cancelled' and (self.applied_at is not None or self.delivery_ref is not None):
            raise ValueError('catalog_attempt_cancelled_invalid')
        if self.state == 'sandbox_verified' and self.proof is not None and self.proof.outcome != 'accepted':
            raise ValueError('catalog_attempt_state_invalid')
        if self.state == 'failed' and self.proof is not None and self.proof.outcome != 'rejected':
            raise ValueError('catalog_attempt_state_invalid')
        return self


CatalogCommandState = Literal[
    'prepared', 'dispatched', 'unknown', 'sandbox_verified', 'failed', 'cancelled',
    'pending', 'approved', 'rejected', 'staged', 'manual_rejected',
]


class CatalogCommandView(StrictDTO):
    id: Identifier
    product_id: Identifier
    operation: Operation
    target_version: Version
    source_digest: Digest
    action_digest: Digest
    context_digest: Digest
    source_snapshot: CatalogContext
    proposal_digest: Digest
    created_at: Timestamp
    request: CatalogERPCommandOut
    state: CatalogCommandState
    attempt: CatalogAttemptView | None
    mode: Literal['synthetic']
    effect: Literal['local_sandbox_only']
    catalog_state: Literal['not_verified']
    replayed: bool = Field(strict=True)

    @model_validator(mode='after')
    def bindings_match(self):
        source = self.source_snapshot.source
        action = self.source_snapshot.action
        if (source.product_id != self.product_id or source.target_version != self.target_version
                or action.operation != self.operation
                or canonical_digest(source.model_dump(mode='json')) != self.source_digest
                or canonical_digest(action.model_dump(mode='json')) != self.action_digest
                or canonical_digest(self.source_snapshot.model_dump(mode='json')) != self.context_digest):
            raise ValueError('catalog_command_source_binding_invalid')

        request = self.request
        details = request.request
        parameters = details.business_parameters
        if (details.action != 'catalog_maintenance_proposal'
                or details.target_id != self.product_id
                or details.target_version != self.target_version
                or details.source_sha256 != self.context_digest
                or request.target_digest != self.source_digest
                or parameters != CatalogERPParameters(
                    operation=self.operation, action_digest=self.action_digest,
                    context_digest=self.context_digest)):
            raise ValueError('catalog_command_request_binding_invalid')

        if self.attempt is None:
            if self.state != request.state:
                raise ValueError('catalog_command_state_binding_invalid')
        else:
            attempt = self.attempt
            if self.state != attempt.state:
                raise ValueError('catalog_command_attempt_binding_invalid')
            result = attempt.proof
            if result is not None:
                if result.operation != self.operation or result.action_digest != self.action_digest:
                    raise ValueError('catalog_command_attempt_binding_invalid')
                if result.outcome == 'rejected':
                    if result.remote_version != source.sandbox_remote_version:
                        raise ValueError('catalog_command_attempt_binding_invalid')
                elif self.operation == 'text_update':
                    if (result.field_name != action.field_name or result.field_value != action.new_value
                            or result.remote_version != source.sandbox_remote_version + 1):
                        raise ValueError('catalog_command_attempt_binding_invalid')
                elif self.operation == 'image_upload':
                    if (result.asset_sha256 != action.asset.sha256
                            or result.remote_version != source.sandbox_remote_version):
                        raise ValueError('catalog_command_attempt_binding_invalid')
                elif (result.asset_sha256 != action.asset.sha256
                        or result.asset_ref != action.upload_proof.asset_ref
                        or result.remote_version != source.sandbox_remote_version + 1):
                    raise ValueError('catalog_command_attempt_binding_invalid')
        return self


class CatalogCommandStatus(StrictDTO):
    status: Literal['found', 'not_found']
    command: CatalogCommandView | None

    @model_validator(mode='after')
    def found_matches_command(self):
        if (self.status == 'found') != (self.command is not None):
            raise ValueError('catalog_command_status_invalid')
        return self


class CatalogObservationView(StrictDTO):
    id: Identifier
    attempt_id: Identifier
    actor_id: Identifier
    kind: Literal['dispatch_intent', 'ack', 'readback', 'network_error', 'cancelled']
    evidence_digest: Digest
    created_at: Timestamp


Cursor = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256,
    pattern=r'^[A-Za-z0-9_-]+={0,2}$')]


def _catalog_cursor(value):
    if value != value.strip():
        raise ValueError('catalog_cursor_invalid')
    unpadded = value.rstrip('=')
    padding = len(value) - len(unpadded)
    if not unpadded or len(unpadded) % 4 == 1:
        raise ValueError('catalog_cursor_invalid')
    if padding and (len(value) % 4 != 0 or padding != (-len(unpadded)) % 4):
        raise ValueError('catalog_cursor_invalid')
    try:
        raw = base64.b64decode(value + '=' * ((-len(value)) % 4), altchars=b'-_', validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('catalog_cursor_invalid') from None
    if base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=') != unpadded:
        raise ValueError('catalog_cursor_invalid')
    return value


StrictCursor = Annotated[Cursor, AfterValidator(_catalog_cursor)]


class CatalogObservationPage(StrictDTO):
    command_id: Identifier
    attempt_id: Identifier | None
    items: list[CatalogObservationView] = Field(max_length=100)
    next_cursor: StrictCursor | None

    @model_validator(mode='after')
    def page_matches_attempt(self):
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError('catalog_observation_duplicate_id')
        if self.items and self.attempt_id is None:
            raise ValueError('catalog_observation_attempt_required')
        if any(item.attempt_id != self.attempt_id for item in self.items):
            raise ValueError('catalog_observation_attempt_mismatch')
        if self.attempt_id is None and self.next_cursor is not None:
            raise ValueError('catalog_observation_cursor_without_attempt')
        return self


ApiError = Annotated[str, StringConstraints(strict=True, max_length=1000)]
ApiMessage = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]


class CatalogApiEnvelope(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True,
        revalidate_instances='always', hide_input_in_errors=True)

    success: bool = Field(strict=True)
    data: object | None
    code: ErrorCode
    error: ApiError | None
    user_message: ApiMessage
    hint: ApiMessage | None

    @model_validator(mode='after')
    def success_shape_matches(self):
        if self.success:
            if self.code != 'OK' or self.error is not None or self.data is None:
                raise ValueError('catalog_envelope_success_invalid')
        elif self.data is not None or not self.error or self.code == 'OK':
            raise ValueError('catalog_envelope_failure_invalid')
        return self


class CatalogContextEnvelope(CatalogApiEnvelope):
    data: CatalogContextView | None


class CatalogCommandEnvelope(CatalogApiEnvelope):
    data: CatalogCommandView | None


class CatalogCommandStatusEnvelope(CatalogApiEnvelope):
    data: CatalogCommandStatus | None


class CatalogObservationPageEnvelope(CatalogApiEnvelope):
    data: CatalogObservationPage | None


class CatalogCancelInput(StrictDTO):
    """Explicit local cancellation, strictly before any durable dispatch marker."""
    expected_attempt_version: Version
    input_digest: Digest
    reason: Reason
