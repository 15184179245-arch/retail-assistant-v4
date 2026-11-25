"""Scoped source observations and byte integrity, never writes source business data."""
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import os
import stat
import json
import hmac

from pydantic import ValidationError
from sqlalchemy import select, text

from backend.app.modules.auth.service import has_usable_account
from backend.app.modules.identity.command_guard import lock_employee_authority
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.catalog.models import CatalogProduct, CatalogSnapshot, CatalogExtension
from backend.app.modules.media.models import MediaAsset
from backend.app.modules.media import local_storage
from backend.app.modules.erp_commands.erp_003_wire import canonical_digest, SandboxAdapterError
from backend.app.modules.erp_catalog.erp_007_models import ERPCatalogHead, ERPCatalogCommand, ERPCatalogAttempt
from backend.app.modules.erp_catalog.erp_007_schemas import (
    CatalogSource, TextFields, AssetSnapshot, TextUpdateInput, ImageUploadInput, ImageBindInput,
    TextAction, UploadAction, BindAction, UploadProof, CatalogContext, CatalogContextView, CatalogResult,
)


class ERPCatalogError(ValueError):
    def __init__(self, code):
        self.code = 'erp_catalog_' + code
        super().__init__(self.code)


def fail(code):
    raise ERPCatalogError(code)


def clock(db):
    sql = "SELECT clock_timestamp()" if db.get_bind().dialect.name == 'postgresql' else "SELECT strftime('%Y-%m-%d %H:%M:%f','now')"
    value = db.execute(text(sql)).scalar_one()
    value = datetime.fromisoformat(value) if type(value) is str else value
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def scope(model, actor):
    return (model.organization_id == actor.organization_id, model.store_id == actor.store_id)


def require_person(db, actor, permission, now):
    if (not getattr(actor, 'account_id', None) or not getattr(actor, 'session_id', None)
            or type(getattr(actor, 'account_version', None)) is not int or actor.account_version <= 0):
        fail('human_required')
    authorize_scope(db, actor, organization_id=actor.organization_id, store_id=actor.store_id,
        permission=permission, now=now)
    if not has_usable_account(db, organization_id=actor.organization_id, store_id=actor.store_id,
            subject_id=actor.subject_id, now=now):
        fail('human_required')


def guard(db, actor, permission='erp.catalog.read'):
    if not getattr(actor, 'account_id', None) or not getattr(actor, 'session_id', None):
        fail('human_required')
    lock_employee_authority(db, actor)
    now = clock(db)
    for cap in ('catalog.read', 'erp.command.read', 'erp.catalog.read', permission):
        require_person(db, actor, cap, now)
    return now


def _one(db, model, actor, *conditions):
    return db.scalar(select(model).where(*scope(model, actor), *conditions)
        .execution_options(populate_existing=True).with_for_update())


def head(db, actor, product_id):
    return _one(db, ERPCatalogHead, actor, ERPCatalogHead.id == product_id)


def _observation_sha_matches(payload, source_version, source_updated_at, expected_sha):
    """Verify CAT-002's historical observation encoding without rewriting it.

    CAT-002 hashed the original aware ISO timestamp, then UTCDateTime stored
    its UTC instant. All whole-minute ISO offsets are checked against the
    recorded digest. Hash the potentially large payload ONCE, and copy only
    the small SHA state per candidate. No origin timezone is guessed/stored.
    Unverifiable encodings fail closed rather than repairing old observations.
    """
    if type(expected_sha) is not str:
        return False
    dump = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')
    prefix = sha256(b'{"payload":' + dump(payload) + b',"source_updated_at":')
    suffix = b',"source_version":' + dump(source_version) + b'}'
    def matches(value):
        digest = prefix.copy()
        digest.update(dump(value))
        digest.update(suffix)
        return hmac.compare_digest(digest.hexdigest(), expected_sha)
    if source_updated_at is None:
        return matches(None)
    common = (0, 480, -300, 330, 345, -330)
    for minutes in (*common, *(m for m in range(-1439, 1440) if m not in common)):
        try:
            encoded = source_updated_at.astimezone(timezone(timedelta(minutes=minutes))).isoformat()
        except (ValueError, OverflowError):
            continue
        if matches(encoded):
            return True
    return False


def base_snapshot(db, actor, product_id):
    guard(db, actor)
    product = _one(db, CatalogProduct, actor, CatalogProduct.id == product_id)
    if product is None:
        fail('not_found')
    if not product.active or product.current_snapshot_id is None:
        fail('source_unavailable')
    snapshot = _one(db, CatalogSnapshot, actor, CatalogSnapshot.id == product.current_snapshot_id,
        CatalogSnapshot.product_id == product.id)
    if (snapshot is None or snapshot.source_version != product.source_version
            or snapshot.source_updated_at != product.source_updated_at):
        fail('source_invalid')
    # Lock the parent product before looking for the extension. PostgreSQL FK
    # key-share insertion also conflicts with this lock, covering a first row.
    extension = db.scalar(select(CatalogExtension).where(CatalogExtension.product_id == product.id)
        .execution_options(populate_existing=True).with_for_update())
    if extension is not None and (extension.organization_id, extension.store_id) != (actor.organization_id, actor.store_id):
        fail('source_invalid')
    current = head(db, actor, product.id)
    if current is not None and current.busy_command_id is not None:
        busy = _one(db, ERPCatalogCommand, actor, ERPCatalogCommand.id == current.busy_command_id,
            ERPCatalogCommand.product_id == product.id)
        if busy is None or busy.target_version != current.version:
            fail('target_binding_invalid')
    try:
        if type(snapshot.payload) is not dict or type(snapshot.payload.get('raw')) is not dict:
            fail('source_invalid')
        raw = snapshot.payload['raw']
        if (str(raw.get('code')) != product.external_code
                or snapshot.payload.get('name') != raw.get('name')
                or snapshot.payload.get('spec') != raw.get('spec')):
            fail('source_invalid')
        if not _observation_sha_matches(snapshot.payload, snapshot.source_version, snapshot.source_updated_at, snapshot.payload_sha256):
            fail('snapshot_integrity_invalid')
        original = TextFields(display_label=raw.get('name'), specification_note=raw.get('spec'))
        result = CatalogSource(organization_id=actor.organization_id, store_id=actor.store_id,
            product_id=product.id, product_version=product.version, source_system=product.source_system,
            external_code=product.external_code, source_version=product.source_version,
            source_updated_at=product.source_updated_at, snapshot_id=snapshot.id,
            snapshot_sha256=snapshot.payload_sha256, snapshot_source_updated_at=snapshot.source_updated_at,
            snapshot_captured_at=snapshot.captured_at, snapshot_payload_digest=canonical_digest(snapshot.payload),
            original_fields=original, extension_id=extension.id if extension else None,
            extension_version=extension.version if extension else None,
            extension_digest=canonical_digest(extension.payload) if extension else None,
            target_version=current.version if current else 1, sandbox_remote_version=current.remote_version if current else 1,
            sandbox_fields=TextFields.model_validate(current.fields) if current else original,
            sandbox_image_ref=current.image_ref if current else None,
            real_erp_mapping='unverified', effect='local_sandbox_only')
    except (ValidationError, ValueError, TypeError, SandboxAdapterError) as exc:
        if isinstance(exc, ERPCatalogError):
            raise
        fail('source_invalid')
    for cap in ('catalog.read', 'erp.catalog.read', 'erp.command.read'):
        require_person(db, actor, cap, clock(db))
    return result


def asset_snapshot(db, actor, asset_id):
    require_person(db, actor, 'media.read', clock(db))
    asset = _one(db, MediaAsset, actor, MediaAsset.id == asset_id)
    if asset is None:
        fail('asset_not_found')
    try:
        metadata = AssetSnapshot.model_validate({k: getattr(asset, k) for k in AssetSnapshot.model_fields})
    except (ValidationError, ValueError, TypeError):
        fail('asset_invalid')
    expected = local_storage.storage_key(actor.organization_id, actor.store_id, metadata.sha256)
    if asset.storage_key != expected:
        fail('asset_invalid')
    return metadata


def read_image_bytes(actor, metadata):
    """Exact scoped key, no symlink, bounded regular file; no caller path/URL."""
    root = local_storage.media_root()
    key = local_storage.storage_key(actor.organization_id, actor.store_id, metadata.sha256)
    parts = key.split('/')
    if any(not part or part in ('.', '..') for part in parts):
        fail('asset_content_invalid')
    directory_fd = None
    try:
        # Use directory-relative opens at every component. A check-then-open
        # Path.resolve alone would permit intermediate symlink replacement.
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != metadata.byte_size:
                fail('asset_content_invalid')
            content = stream.read(local_storage.MAX_BYTES + 1)
        if (len(content) != metadata.byte_size or sha256(content).hexdigest() != metadata.sha256
                or local_storage.detect_content_type(content) != metadata.content_type):
            fail('asset_content_invalid')
    except (OSError, local_storage.MediaStorageError):
        fail('asset_content_invalid')
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    return content


def upload_proof(db, actor, product_id, metadata, command_id):
    command = _one(db, ERPCatalogCommand, actor, ERPCatalogCommand.id == command_id,
        ERPCatalogCommand.product_id == product_id, ERPCatalogCommand.operation == 'image_upload')
    if command is None:
        fail('upload_proof_unavailable')
    attempt = _one(db, ERPCatalogAttempt, actor, ERPCatalogAttempt.command_id == command.id)
    if (attempt is None or attempt.state != 'sandbox_verified' or attempt.applied_at is None
            or attempt.receipt_digest is None or attempt.proof is None):
        fail('upload_proof_unavailable')
    try:
        frozen = CatalogContext.model_validate(command.source_snapshot)
        proof = CatalogResult.model_validate(attempt.proof)
        if (frozen.source.product_id != product_id or frozen.source.target_version != command.target_version
                or frozen.action.operation != 'image_upload'
                or frozen.action.asset != metadata or canonical_digest(frozen.model_dump(mode='json')) != command.context_digest
                or canonical_digest(frozen.source.model_dump(mode='json')) != command.source_digest
                or canonical_digest(frozen.action.model_dump(mode='json')) != command.action_digest
                or proof.operation != 'image_upload' or proof.outcome != 'accepted'
                or proof.action_digest != command.action_digest or proof.asset_sha256 != metadata.sha256):
            fail('upload_proof_invalid')
        # A normalized accepted result must be backed by the immutable exact
        # readback observation, not only by mutable Attempt convenience fields.
        from backend.app.modules.erp_catalog.erp_007_models import ERPCatalogObservation
        observed = _one(db, ERPCatalogObservation, actor, ERPCatalogObservation.attempt_id == attempt.id,
            ERPCatalogObservation.kind == 'readback', ERPCatalogObservation.evidence_digest == attempt.receipt_digest)
        if (observed is None or type(observed.evidence) is not dict
                or observed.evidence.get('result') != proof.model_dump(mode='json')
                or canonical_digest(observed.evidence) != observed.evidence_digest):
            fail('upload_proof_invalid')
        from backend.app.modules.erp_catalog.erp_007_wire import CatalogDispatchRequest
        wire = CatalogDispatchRequest.model_validate(observed.evidence.get('request'))
        if (wire.attempt_id != attempt.id or wire.command_id != command.id or wire.request_id != command.request_id
                or wire.input_digest != attempt.input_digest or wire.context != frozen
                or wire.context_digest != command.context_digest or wire.source_digest != command.source_digest
                or wire.action_digest != command.action_digest or wire.organization_id != actor.organization_id
                or wire.store_id != actor.store_id or proof.remote_version != frozen.source.sandbox_remote_version
                or attempt.result_remote_version != proof.remote_version):
            fail('upload_proof_invalid')
        from backend.app.modules.erp_commands.erp_002_models import ERPCommandRequest, ERPCommandIntent, ERPCommandApproval
        request = _one(db, ERPCommandRequest, actor, ERPCommandRequest.id == command.request_id)
        intent = _one(db, ERPCommandIntent, actor, ERPCommandIntent.id == attempt.intent_id)
        approval = None if intent is None else _one(db, ERPCommandApproval, actor, ERPCommandApproval.id == intent.approval_id)
        params = dict(operation='image_upload', action_digest=command.action_digest, context_digest=command.context_digest)
        if (request is None or intent is None or approval is None or request.action != 'catalog_maintenance_proposal'
                or request.target_kind != 'catalog_maintenance_target' or request.target_id != product_id
                or request.expected_version != command.target_version or request.target_digest != command.source_digest
                or request.evidence_digest != command.context_digest or request.parameters != params
                or request.parameters_digest != canonical_digest(params) or request.requested_by != command.created_by
                or intent.request_id != request.id or approval.request_id != request.id or approval.decision != 'approved'
                or intent.executor_id != attempt.executor_id or intent.target_digest != command.source_digest
                or intent.request_digest != request.request_digest or approval.request_digest != request.request_digest
                or wire.request_digest != request.request_digest or attempt.dispatched_at is None
                or not (attempt.created_at <= attempt.dispatched_at < min(attempt.authorization_expires_at,
                    request.expires_at, approval.expires_at)) or attempt.applied_at < attempt.dispatched_at
                or len({command.created_by, approval.approved_by, attempt.executor_id}) != 3):
            fail('upload_proof_invalid')
        expected = canonical_digest(dict(scope=[actor.organization_id, actor.store_id], requested_by=command.created_by,
            action='catalog_maintenance_proposal', target=['catalog_maintenance_target', product_id,
                command.target_version, command.source_digest], source_sha256=command.context_digest,
            parameters=params, expires_at=request.expires_at.isoformat()))
        if expected != request.request_digest:
            fail('upload_proof_invalid')
        return UploadProof(command_id=command.id, attempt_id=attempt.id, product_id=product_id,
            asset_id=metadata.id, asset_sha256=metadata.sha256, byte_size=metadata.byte_size,
            content_type=metadata.content_type, input_digest=attempt.input_digest,
            receipt_digest=attempt.receipt_digest, asset_ref=proof.asset_ref, verification_state='sandbox_verified')
    except (ValidationError, TypeError, ValueError, SandboxAdapterError):
        fail('upload_proof_invalid')


def context(db, actor, product_id, selection):
    base = base_snapshot(db, actor, product_id)
    try:
        if isinstance(selection, TextUpdateInput):
            selection = TextUpdateInput.model_validate(selection.model_dump())
            action = TextAction(**selection.model_dump(), original_value=getattr(base.original_fields, selection.field_name),
                current_value=getattr(base.sandbox_fields, selection.field_name))
        elif isinstance(selection, ImageUploadInput):
            selection = ImageUploadInput.model_validate(selection.model_dump())
            metadata = asset_snapshot(db, actor, selection.asset_id)
            read_image_bytes(actor, metadata)
            action = UploadAction(operation='image_upload', asset=metadata)
        elif isinstance(selection, ImageBindInput):
            selection = ImageBindInput.model_validate(selection.model_dump())
            metadata = asset_snapshot(db, actor, selection.asset_id)
            action = BindAction(operation='image_bind', asset=metadata,
                upload_proof=upload_proof(db, actor, product_id, metadata, selection.upload_command_id))
        else:
            fail('input_invalid')
        result = CatalogContext(source=base, action=action)
    except (ValidationError, TypeError, ValueError, SandboxAdapterError) as exc:
        if isinstance(exc, ERPCatalogError):
            raise
        fail('context_invalid')
    for cap in ('catalog.read', 'erp.catalog.read', 'erp.command.read'):
        require_person(db, actor, cap, clock(db))
    if selection.operation != 'text_update':
        require_person(db, actor, 'media.read', clock(db))
    return result


def context_view(value):
    return CatalogContextView(context=value,
        source_digest=canonical_digest(value.source.model_dump(mode='json')),
        action_digest=canonical_digest(value.action.model_dump(mode='json')),
        context_digest=canonical_digest(value.model_dump(mode='json'))).model_dump()
