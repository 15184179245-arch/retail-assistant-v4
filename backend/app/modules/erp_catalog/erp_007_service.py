"""Per-action synthetic catalog approval, durable dispatch and scoped projection.

All local operations use the caller's transaction. Network entry points commit
an irreversible marker before HTTP and never hold database locks over the wire.
Original catalog and media records are observations, never mutation targets.
"""
from hashlib import sha256
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select

from backend.app.modules.audit.service import record_event
from backend.app.modules.auth.service import has_usable_account
from backend.app.modules.identity.repository import load_scope, grants_for
from backend.app.modules.identity.service import permission_allowed
from backend.app.modules.identity.command_guard import lock_scope_security_fence
from backend.app.modules.erp_commands import erp_002_service as gateway
from backend.app.modules.erp_commands.erp_002_schemas import ERPCommandPropose, ERPCommandDecide, ERPCommandStage
from backend.app.modules.erp_commands.erp_003_wire import canonical_digest, SandboxAdapterError
from backend.app.modules.erp_catalog import erp_007_source as source
from backend.app.modules.erp_catalog.erp_007_models import (
    ERPCatalogHead, ERPCatalogCommand, ERPCatalogAttempt, ERPCatalogObservation,
)
from backend.app.modules.erp_catalog.erp_007_schemas import (
    CatalogContext, CatalogPreviewInput, CatalogProposeInput, CatalogDecideInput, CatalogStageInput,
    TextUpdateInput, ImageUploadInput, ImageBindInput,
)
from backend.app.modules.erp_catalog.erp_007_wire import (
    CatalogDispatchRequest, CatalogDispatchAck, CatalogReadbackReceipt,
    dispatch_input_digest, normalized_proof,
)

ERPCatalogError = source.ERPCatalogError
ACTION = 'catalog_maintenance_proposal'
EVENT_TYPE = 'erp.catalog.dispatch.v1'


def _typed(model, body):
    if type(body) is not model:
        source.fail('input_invalid')
    try:
        return model.model_validate(body.model_dump(warnings=False))
    except (ValidationError, ValueError, TypeError):
        source.fail('input_invalid')


def _key(value):
    if type(value) is not str or not 1 <= len(value) <= 200 or value != value.strip() or any(ord(c) < 32 for c in value):
        source.fail('key_invalid')
    return value


def _guard(db, actor, capability):
    return source.guard(db, actor, 'erp.catalog.' + capability)


def _allow(db, actor, permission, now):
    source.require_person(db, actor, permission, now)


def _operation(db, actor, operation, now):
    _allow(db, actor, 'erp.catalog.' + operation, now)
    if operation != 'text_update':
        _allow(db, actor, 'media.read', now)


def _permissions(operation, capability):
    result = ['catalog.read', 'erp.command.read', 'erp.catalog.read',
        'erp.catalog.' + capability, 'erp.catalog.' + operation,
        'erp.command.' + ('stage' if capability == 'dispatch' else capability)]
    if operation != 'text_update':
        result.append('media.read')
    return result


def _participant(db, actor, ident, permissions, now):
    org, store, subject, roles = load_scope(db, actor.organization_id, actor.store_id, ident)
    if (not org or not org.active or not store or not store.active or store.organization_id != actor.organization_id
            or not subject or not subject.active or subject.organization_id != actor.organization_id
            or subject.subject_type != 'person' or not has_usable_account(db, organization_id=actor.organization_id,
                store_id=actor.store_id, subject_id=ident, now=now)):
        source.fail('participant_unavailable')
    for permission in permissions:
        if not permission_allowed(grants_for(db, actor.organization_id, actor.store_id, ident, roles, permission), ident, now=now):
            source.fail('participant_unavailable')


def _command(db, actor, ident):
    row = db.scalar(select(ERPCatalogCommand).where(*source.scope(ERPCatalogCommand, actor), ERPCatalogCommand.id == ident)
        .execution_options(populate_existing=True))
    if row is None:
        source.fail('not_found')
    request = gateway._request(db, actor, row.request_id)
    try:
        frozen = CatalogContext.model_validate(row.source_snapshot)
        view = source.context_view(frozen)
    except (ValidationError, TypeError, ValueError):
        source.fail('binding_changed')
    params = dict(operation=row.operation, action_digest=row.action_digest, context_digest=row.context_digest)
    expected = canonical_digest(dict(scope=[actor.organization_id, actor.store_id], requested_by=row.created_by,
        action=ACTION, target=['catalog_maintenance_target', row.product_id, row.target_version, row.source_digest],
        source_sha256=row.context_digest, parameters=params, expires_at=request.expires_at.isoformat()))
    if (request.action != ACTION or request.target_kind != 'catalog_maintenance_target' or request.target_id != row.product_id
            or request.expected_version != row.target_version or request.requested_by != row.created_by
            or request.target_digest != row.source_digest or request.evidence_digest != row.context_digest
            or request.parameters != params or request.parameters_digest != canonical_digest(params)
            or request.request_digest != expected or view['source_digest'] != row.source_digest
            or view['action_digest'] != row.action_digest or view['context_digest'] != row.context_digest
            or frozen.action.operation != row.operation
            or (frozen.source.organization_id, frozen.source.store_id, frozen.source.product_id,
                frozen.source.target_version) != (row.organization_id, row.store_id, row.product_id, row.target_version)):
        source.fail('binding_changed')
    return row, request, frozen


def _attempt(db, actor, row):
    return db.scalar(select(ERPCatalogAttempt).where(*source.scope(ERPCatalogAttempt, actor),
        ERPCatalogAttempt.command_id == row.id).execution_options(populate_existing=True).with_for_update())


def _view(db, actor, row, request, *, replayed=False):
    _guard(db, actor, 'read')
    if row.operation != 'text_update':
        _allow(db, actor, 'media.read', source.clock(db))
    attempt = _attempt(db, actor, row)
    details = gateway._response(db, actor, request)
    # Keep this response explicit: no account/session credentials or blob bytes.
    value = dict(id=row.id, product_id=row.product_id, operation=row.operation, target_version=row.target_version,
        source_digest=row.source_digest, action_digest=row.action_digest, context_digest=row.context_digest,
        source_snapshot=row.source_snapshot, proposal_digest=row.proposal_digest, created_at=row.created_at,
        request=details, state=attempt.state if attempt else details['state'],
        attempt=None if attempt is None else {k: getattr(attempt, k) for k in (
            'id', 'intent_id', 'executor_id', 'state', 'version', 'input_digest', 'proof',
            'result_remote_version', 'applied_at', 'delivery_ref', 'external_ref', 'receipt_digest',
            'created_at', 'updated_at', 'dispatched_at')},
        mode='synthetic', effect='local_sandbox_only', catalog_state='not_verified', replayed=replayed)
    from backend.app.modules.erp_catalog.erp_007_schemas import CatalogCommandView
    return CatalogCommandView.model_validate(value).model_dump()


def _finish(db, actor, row, request, capability, *, replayed=False):
    value = _view(db, actor, row, request, replayed=replayed)
    now = source.clock(db)
    _allow(db, actor, 'erp.catalog.' + capability, now)
    if capability != 'read':
        _operation(db, actor, row.operation, now)
    return value


def _audit(db, actor, operation, row, key, details, now):
    record_event(db, actor_id=actor.subject_id, organization_id=actor.organization_id, store_id=actor.store_id,
        action='erp.catalog.' + operation, target_type='erp_catalog_command', target_id=row.id,
        idempotency_key='erp7:' + canonical_digest(dict(scope=[actor.organization_id, actor.store_id],
            operation=operation, key=key)), details=details, occurred_at=now)


def preview_catalog_command(db, actor, body):
    body = _typed(CatalogPreviewInput, body)
    _guard(db, actor, 'read')
    return source.context_view(source.context(db, actor, body.product_id, body.action))


def read_catalog_command(db, actor, ident):
    _guard(db, actor, 'read')
    row, request, _ = _command(db, actor, ident)
    return _view(db, actor, row, request)


def _selection(frozen):
    action = frozen.action
    if action.operation == 'text_update':
        return TextUpdateInput(operation=action.operation, field_name=action.field_name, new_value=action.new_value)
    if action.operation == 'image_upload':
        return ImageUploadInput(operation=action.operation, asset_id=action.asset.id)
    return ImageBindInput(operation=action.operation, asset_id=action.asset.id,
        upload_command_id=action.upload_proof.command_id)


def _current(db, actor, row, frozen):
    current = source.context(db, actor, row.product_id, _selection(frozen))
    if source.context_view(current)['context_digest'] != row.context_digest:
        source.fail('source_changed')
    return current


def propose_catalog_command(db, actor, body, key):
    body, key = _typed(CatalogProposeInput, body), _key(key)
    now = _guard(db, actor, 'propose')
    _operation(db, actor, body.action.operation, now)
    _allow(db, actor, 'erp.command.propose', now)
    digest = canonical_digest(dict(scope=[actor.organization_id, actor.store_id], actor_id=actor.subject_id,
        operation='propose', input=body.model_dump(mode='json')))
    old = db.scalar(select(ERPCatalogCommand).where(*source.scope(ERPCatalogCommand, actor), ERPCatalogCommand.proposal_key == key))
    if old is not None:
        if old.created_by != actor.subject_id or old.proposal_digest != digest:
            source.fail('idempotency_conflict')
        row, request, _ = _command(db, actor, old.id)
        return _finish(db, actor, row, request, 'propose', replayed=True)
    frozen = source.context(db, actor, body.product_id, body.action)
    view = source.context_view(frozen)
    if frozen.source.target_version != body.expected_target_version or view['context_digest'] != body.expected_context_digest:
        source.fail('source_changed')
    head = source.head(db, actor, body.product_id)
    if head is None:
        head = ERPCatalogHead(id=body.product_id, organization_id=actor.organization_id, store_id=actor.store_id,
            version=1, remote_version=1, fields=frozen.source.sandbox_fields.model_dump(), image_ref=None,
            busy_command_id=None, created_at=now, updated_at=now)
        db.add(head)
        db.flush()
    params = dict(operation=body.action.operation, action_digest=view['action_digest'], context_digest=view['context_digest'])
    proposal = gateway.propose_command(db, actor, ERPCommandPropose(action=ACTION, target_id=body.product_id,
        target_version=head.version, source_sha256=view['context_digest'], business_parameters=params,
        expires_at=body.expires_at), 'erp7:' + canonical_digest(dict(key=key,
            scope=[actor.organization_id, actor.store_id])), now=source.clock(db))
    request = gateway._request(db, actor, proposal['id'])
    row = ERPCatalogCommand(id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        product_id=body.product_id, request_id=request.id, operation=body.action.operation,
        target_version=head.version, source_digest=view['source_digest'], action_digest=view['action_digest'],
        context_digest=view['context_digest'], source_snapshot=frozen.model_dump(mode='json'),
        proposal_key=key, proposal_digest=digest, created_by=actor.subject_id, created_at=request.created_at)
    db.add(row)
    db.flush()
    _audit(db, actor, 'proposed', row, key, dict(request_digest=request.request_digest,
        context_digest=row.context_digest, reason_digest=sha256(body.reason.encode()).hexdigest()), source.clock(db))
    _command(db, actor, row.id)
    _current(db, actor, row, frozen)
    return _finish(db, actor, row, request, 'propose')


def decide_catalog_command(db, actor, ident, body, key):
    body, key = _typed(CatalogDecideInput, body), _key(key)
    now = _guard(db, actor, 'approve')
    row, request, frozen = _command(db, actor, ident)
    _operation(db, actor, row.operation, now)
    _participant(db, actor, row.created_by, _permissions(row.operation, 'propose'), now)
    # Replays return the immutable approval; new approvals revalidate the complete
    # image/source/proof, beyond the generic ledger's target-only check.
    if gateway._approval(db, actor, request) is None:
        _current(db, actor, row, frozen)
    result = gateway.decide_command(db, actor, row.request_id, ERPCommandDecide(**body.model_dump()),
        'erp7:' + canonical_digest(dict(scope=[actor.organization_id, actor.store_id], key=key)), now=now)
    if not result['replayed'] and source.clock(db) >= min(request.expires_at, body.expires_at):
        source.fail('approval_expired')
    return _finish(db, actor, row, request, 'approve', replayed=result['replayed'])


def _approved_intent(db, actor, row, request, frozen, now):
    approval, intent = gateway._approval(db, actor, request), gateway._intent(db, actor, request)
    if (approval is None or intent is None or approval.decision != 'approved' or intent.approval_id != approval.id
            or intent.executor_id != actor.subject_id or len({row.created_by, approval.approved_by, intent.executor_id}) != 3
            or intent.request_digest != request.request_digest or approval.request_digest != request.request_digest
            or intent.target_digest != row.source_digest or gateway._outcome(db, actor, intent) is not None):
        source.fail('approval_invalid')
    if now >= min(request.expires_at, approval.expires_at):
        source.fail('approval_expired')
    _participant(db, actor, row.created_by, _permissions(row.operation, 'propose'), now)
    _participant(db, actor, approval.approved_by, _permissions(row.operation, 'approve'), now)
    _current(db, actor, row, frozen)
    return intent, approval


def _wire(row, request, attempt):
    frozen = CatalogContext.model_validate(row.source_snapshot)
    value = dict(contract_id='retail.erp-catalog-sandbox', adapter_id='synthetic.catalog.v1', protocol_version=1,
        mode='synthetic', organization_id=row.organization_id, store_id=row.store_id, route='api',
        attempt_id=attempt.id, command_id=row.id, request_id=request.id, request_digest=request.request_digest,
        operation=row.operation, source_digest=row.source_digest, action_digest=row.action_digest,
        context_digest=row.context_digest, context=frozen.model_dump(mode='json'))
    value['input_digest'] = dispatch_input_digest(value)
    if attempt.input_digest and attempt.input_digest != value['input_digest']:
        source.fail('binding_changed')
    return CatalogDispatchRequest.model_validate(value)


def _busy(db, actor, row):
    head = source.head(db, actor, row.product_id)
    if head is None or head.version != row.target_version:
        source.fail('source_changed')
    if head.busy_command_id not in (None, row.id):
        source.fail('target_busy')
    return head


def stage_catalog_command(db, actor, ident, body, key):
    body, key = _typed(CatalogStageInput, body), _key(key)
    now = _guard(db, actor, 'dispatch')
    for permission in ('erp.command.stage', 'erp.catalog.reconcile'):
        _allow(db, actor, permission, now)
    row, request, frozen = _command(db, actor, ident)
    _operation(db, actor, row.operation, now)
    digest = canonical_digest(dict(scope=[actor.organization_id, actor.store_id], actor_id=actor.subject_id,
        command_id=row.id, input=body.model_dump(mode='json')))
    old = db.scalar(select(ERPCatalogAttempt).where(*source.scope(ERPCatalogAttempt, actor), ERPCatalogAttempt.queue_key == key))
    if old is not None:
        if old.command_id != row.id or old.executor_id != actor.subject_id or old.queue_digest != digest:
            source.fail('idempotency_conflict')
        return _finish(db, actor, row, request, 'dispatch', replayed=True)
    if _attempt(db, actor, row) is not None:
        source.fail('intent_already_bound')
    head = _busy(db, actor, row)
    _current(db, actor, row, frozen)
    gateway.stage_command(db, actor, request.id, ERPCommandStage(**body.model_dump()),
        'erp7:' + canonical_digest(dict(scope=[actor.organization_id, actor.store_id], key=key)), now=now)
    intent, approval = _approved_intent(db, actor, row, request, frozen, source.clock(db))
    now = source.clock(db)
    expiry = min(actor.auth_expires_at, request.expires_at, approval.expires_at)
    if now >= expiry:
        source.fail('approval_expired')
    attempt = ERPCatalogAttempt(id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        command_id=row.id, intent_id=intent.id, queue_key=key, queue_digest=digest, input_digest='',
        executor_id=actor.subject_id, account_id=actor.account_id, session_id=actor.session_id,
        account_version=actor.account_version, authorization_epoch=lock_scope_security_fence(db, actor.organization_id, actor.store_id),
        authorization_expires_at=expiry, state='prepared', version=1, proof=None, result_remote_version=None,
        applied_at=None, delivery_ref=None, external_ref=None, receipt_digest=None,
        created_at=now, updated_at=now, dispatched_at=None)
    attempt.input_digest = _wire(row, request, attempt).input_digest
    head.busy_command_id, head.updated_at = row.id, now
    db.add(attempt)
    db.flush()
    from backend.app.workers.outbox import enqueue
    enqueue(db, organization_id=actor.organization_id, store_id=actor.store_id,
        event_type=EVENT_TYPE + '.' + actor.subject_id, idempotency_key='erp7:' + attempt.id,
        payload=dict(attempt_id=attempt.id, input_digest=attempt.input_digest), now=now)
    _audit(db, actor, 'queued', row, key, dict(attempt_id=attempt.id, intent_id=intent.id, input_digest=attempt.input_digest), now)
    _approved_intent(db, actor, row, request, frozen, source.clock(db))
    return _finish(db, actor, row, request, 'dispatch')


def _observation(db, actor, row, attempt, kind, key, evidence, now):
    digest = canonical_digest(evidence)
    old = db.scalar(select(ERPCatalogObservation).where(*source.scope(ERPCatalogObservation, actor),
        ERPCatalogObservation.attempt_id == attempt.id, ERPCatalogObservation.observation_key == key))
    if old is not None:
        if old.evidence_digest != digest or old.evidence != evidence or old.kind != kind:
            source.fail('receipt_conflict')
        return old, False
    item = ERPCatalogObservation(id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        attempt_id=attempt.id, observation_key=key, kind=kind, actor_id=actor.subject_id,
        evidence_digest=digest, evidence=evidence, created_at=now)
    db.add(item)
    _audit(db, actor, kind, row, attempt.id + ':' + key, dict(attempt_id=attempt.id, evidence_digest=digest,
        mode='synthetic'), now)
    db.flush()
    return item, True


def prepare_dispatch(db, actor, ident):
    now = _guard(db, actor, 'dispatch')
    for permission in ('erp.command.stage', 'erp.catalog.reconcile'):
        _allow(db, actor, permission, now)
    row, request, frozen = _command(db, actor, ident)
    _operation(db, actor, row.operation, now)
    attempt = _attempt(db, actor, row)
    if attempt is None:
        source.fail('not_staged')
    if attempt.dispatched_at is not None:
        return None
    if attempt.state != 'prepared':
        source.fail('attempt_state_invalid')
    if (attempt.executor_id, attempt.account_id, attempt.session_id, attempt.account_version) != (
            actor.subject_id, actor.account_id, actor.session_id, actor.account_version):
        source.fail('original_executor_required')
    if (attempt.authorization_epoch != lock_scope_security_fence(db, actor.organization_id, actor.store_id)
            or now >= attempt.authorization_expires_at):
        source.fail('authorization_changed')
    if _busy(db, actor, row).busy_command_id != row.id:
        source.fail('target_binding_invalid')
    intent, _ = _approved_intent(db, actor, row, request, frozen, now)
    if intent.id != attempt.intent_id:
        source.fail('binding_changed')
    wire = _wire(row, request, attempt)
    image_bytes = source.read_image_bytes(actor, frozen.action.asset) if row.operation == 'image_upload' else None
    attempt.state, attempt.dispatched_at, attempt.updated_at = 'dispatched', now, now
    attempt.version += 1
    db.flush()
    _observation(db, actor, row, attempt, 'dispatch_intent', 'dispatch-intent',
        dict(attempt_id=attempt.id, input_digest=attempt.input_digest, operation=row.operation), now)
    final = source.clock(db)
    for permission in ('erp.catalog.dispatch', 'erp.catalog.reconcile'):
        _allow(db, actor, permission, final)
    _operation(db, actor, row.operation, final)
    if final >= attempt.authorization_expires_at:
        source.fail('authorization_changed')
    _approved_intent(db, actor, row, request, frozen, final)
    return wire, image_bytes


def _configured(adapter, actor):
    from backend.app.modules.erp_catalog.erp_007_adapter import SyntheticCatalogHTTPAdapter
    if (type(adapter) is not SyntheticCatalogHTTPAdapter or adapter.mode != 'synthetic'
            or adapter.adapter_id != 'synthetic.catalog.v1' or (adapter.organization_id, adapter.store_id, adapter.route) != (
                actor.organization_id, actor.store_id, 'api')):
        source.fail('adapter_unconfigured')


def _dispatched(db, actor, ident, wire):
    row, request, frozen = _command(db, actor, ident)
    attempt = _attempt(db, actor, row)
    if attempt is None or attempt.dispatched_at is None or _wire(row, request, attempt) != wire:
        source.fail('binding_changed')
    intent, approval = gateway._intent(db, actor, request), gateway._approval(db, actor, request)
    if (intent is None or approval is None or intent.id != attempt.intent_id or intent.approval_id != approval.id
            or intent.request_id != request.id or approval.request_id != request.id or approval.decision != 'approved'
            or intent.executor_id != attempt.executor_id or len({row.created_by, approval.approved_by, attempt.executor_id}) != 3
            or intent.request_digest != request.request_digest or approval.request_digest != request.request_digest
            or intent.target_digest != row.source_digest or gateway._outcome(db, actor, intent) is not None
            or not (attempt.created_at <= attempt.dispatched_at < min(attempt.authorization_expires_at,
                request.expires_at, approval.expires_at))):
        source.fail('binding_changed')
    return row, request, frozen, attempt


def _apply_ack(db, actor, ident, wire, ack):
    now = _guard(db, actor, 'reconcile')
    row, request, _, attempt = _dispatched(db, actor, ident, wire)
    _operation(db, actor, row.operation, now)
    ack = CatalogDispatchAck.model_validate(ack.model_dump())
    for name in ('organization_id', 'store_id', 'route', 'operation', 'attempt_id', 'command_id', 'input_digest'):
        if getattr(ack, name) != getattr(wire, name):
            source.fail('receipt_invalid')
    if attempt.delivery_ref is not None and attempt.delivery_ref != ack.delivery_ref:
        source.fail('receipt_conflict')
    evidence = ack.model_dump(mode='json', exclude={'nonce', 'signature'})
    _, created = _observation(db, actor, row, attempt, 'ack', 'ack:' + canonical_digest(evidence), evidence, now)
    if created:
        attempt.delivery_ref, attempt.updated_at = ack.delivery_ref, now
        attempt.version += 1
        db.flush()
    return _finish(db, actor, row, request, 'reconcile', replayed=not created)


def _apply_readback(db, actor, ident, wire, receipt):
    now = _guard(db, actor, 'reconcile')
    row, request, frozen, attempt = _dispatched(db, actor, ident, wire)
    _operation(db, actor, row.operation, now)
    if receipt is not None:
        receipt = CatalogReadbackReceipt.model_validate(receipt.model_dump())
        if receipt.request != wire:
            source.fail('receipt_invalid')
        evidence = normalized_proof(wire, receipt.result)
    else:
        evidence = dict(attempt_id=attempt.id, input_digest=attempt.input_digest, result='not_found_unknown')
    digest = canonical_digest(evidence)
    # A terminal proof cannot be overwritten, and a later missing receipt must
    # not inspect or mutate a Head that another completed action has advanced.
    if attempt.applied_at is not None:
        if receipt is not None and (attempt.receipt_digest != digest or attempt.proof != receipt.result.model_dump(mode='json')):
            source.fail('receipt_conflict')
        return _finish(db, actor, row, request, 'reconcile', replayed=True)
    head = _busy(db, actor, row)
    if head.busy_command_id != row.id:
        source.fail('target_binding_invalid')
    if (head.remote_version != frozen.source.sandbox_remote_version or head.fields != frozen.source.sandbox_fields.model_dump()
            or head.image_ref != frozen.source.sandbox_image_ref):
        source.fail('target_binding_invalid')
    _, created = _observation(db, actor, row, attempt, 'readback', 'readback:' + digest, evidence, now)
    if receipt is None:
        if created:
            attempt.state, attempt.updated_at = 'unknown', now
            attempt.version += 1
    else:
        result = receipt.result
        if result.outcome == 'accepted':
            if row.operation == 'text_update':
                head.fields = {**head.fields, result.field_name: result.field_value}
            elif row.operation == 'image_bind':
                head.image_ref = result.asset_ref
        head.remote_version, head.version = result.remote_version, head.version + 1
        head.busy_command_id, head.updated_at = None, now
        attempt.state = 'sandbox_verified' if result.outcome == 'accepted' else 'failed'
        attempt.proof, attempt.receipt_digest = result.model_dump(mode='json'), digest
        attempt.external_ref, attempt.result_remote_version = result.asset_ref, result.remote_version
        attempt.applied_at, attempt.updated_at = now, now
        attempt.version += 1
    db.flush()
    return _finish(db, actor, row, request, 'reconcile', replayed=not created)


def _network_error(factory, actor, ident, error):
    with factory.begin() as db:
        now = _guard(db, actor, 'reconcile')
        row, request, _ = _command(db, actor, ident)
        attempt = _attempt(db, actor, row)
        if attempt is None or attempt.dispatched_at is None:
            source.fail('not_dispatched')
        _dispatched(db, actor, ident, _wire(row, request, attempt))
        _operation(db, actor, row.operation, now)
        evidence = dict(code=error.code, phase=error.phase, maybe_sent=error.maybe_sent)
        _, created = _observation(db, actor, row, attempt, 'network_error', 'error:' + canonical_digest(evidence), evidence, now)
        if created and attempt.applied_at is None:
            attempt.state, attempt.updated_at = 'unknown', now
            attempt.version += 1
            db.flush()
        return _finish(db, actor, row, request, 'reconcile', replayed=not created)


def dispatch_catalog_command(factory, actor, ident, adapter=None, *, enabled=False):
    if type(enabled) is not bool:
        source.fail('input_invalid')
    if not enabled:
        source.fail('dispatch_disabled')
    with factory.begin() as db:
        _guard(db, actor, 'dispatch')
        row, request, _ = _command(db, actor, ident)
        _configured(adapter, actor)
        prepared = prepare_dispatch(db, actor, ident)
        if prepared is not None:
            wire, image_bytes = prepared
    if prepared is None:
        return reconcile_catalog_command(factory, actor, ident, adapter)
    try:
        ack = adapter.dispatch(wire, image_bytes=image_bytes)
    except SandboxAdapterError as error:
        return _network_error(factory, actor, ident, error)
    with factory.begin() as db:
        _apply_ack(db, actor, ident, wire, ack)
    return reconcile_catalog_command(factory, actor, ident, adapter)


def reconcile_catalog_command(factory, actor, ident, adapter=None):
    with factory.begin() as db:
        now = _guard(db, actor, 'reconcile')
        row, request, _ = _command(db, actor, ident)
        _operation(db, actor, row.operation, now)
        _configured(adapter, actor)
        attempt = _attempt(db, actor, row)
        if attempt is None or attempt.dispatched_at is None:
            source.fail('not_dispatched')
        wire = _wire(row, request, attempt)
        _dispatched(db, actor, ident, wire)
    try:
        receipt = adapter.readback(wire)
    except SandboxAdapterError as error:
        return _network_error(factory, actor, ident, error)
    with factory.begin() as db:
        return _apply_readback(db, actor, ident, wire, receipt)

def command_status(db,actor,key,proposal_digest):
    import re
    from backend.app.modules.erp_catalog.erp_007_schemas import CatalogCommandStatus
    key=_key(key)
    if type(proposal_digest) is not str or re.fullmatch(r'[0-9a-f]{64}',proposal_digest) is None:
        source.fail('input_invalid')
    _guard(db,actor,'read')
    _allow(db,actor,'erp.command.read',source.clock(db))
    row=db.scalar(select(ERPCatalogCommand).where(*source.scope(ERPCatalogCommand,actor),ERPCatalogCommand.proposal_key==key))
    if row is None:
        return CatalogCommandStatus(status='not_found',command=None).model_dump()
    if row.created_by!=actor.subject_id or row.proposal_digest!=proposal_digest:
        source.fail('not_found')
    row,request,_=_command(db,actor,row.id)
    return CatalogCommandStatus(status='found',command=_view(db,actor,row,request,replayed=True)).model_dump()


def read_observations(db,actor,ident,*,limit=50,cursor=None):
    import base64
    import json
    from datetime import datetime
    from pydantic import TypeAdapter
    from sqlalchemy import and_,or_
    from backend.app.modules.erp_catalog.erp_007_schemas import (
        Identifier,StrictCursor,CatalogObservationView,CatalogObservationPage,
    )
    if type(limit) is not int or not 1<=limit<=100:
        source.fail('input_invalid')
    _guard(db,actor,'read')
    _allow(db,actor,'erp.command.read',source.clock(db))
    row,request,_=_command(db,actor,ident)
    if row.operation != 'text_update':
        _allow(db,actor,'media.read',source.clock(db))
    attempt=_attempt(db,actor,row)
    if attempt is None:
        if cursor is not None:
            source.fail('cursor_invalid')
        return CatalogObservationPage(command_id=row.id,attempt_id=None,items=[],next_cursor=None).model_dump()
    fingerprint=canonical_digest(dict(scope=[actor.organization_id,actor.store_id],actor=actor.subject_id,
        command_id=row.id,attempt_id=attempt.id))
    query=select(ERPCatalogObservation).where(*source.scope(ERPCatalogObservation,actor),
        ERPCatalogObservation.attempt_id==attempt.id)
    if cursor is not None:
        try:
            TypeAdapter(StrictCursor).validate_python(cursor)
            payload=json.loads(base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4)))
            if type(payload) is not dict or set(payload)!={'q','t','i'} or payload['q']!=fingerprint:
                raise ValueError()
            at=datetime.fromisoformat(payload['t'])
            if at.tzinfo is None or at.utcoffset() is None:
                raise ValueError()
            item_id=TypeAdapter(Identifier).validate_python(payload['i'])
        except (ValueError,TypeError,KeyError,UnicodeError,ValidationError):
            source.fail('cursor_invalid')
        query=query.where(or_(ERPCatalogObservation.created_at>at,and_(ERPCatalogObservation.created_at==at,
            ERPCatalogObservation.id>item_id)))
    rows=list(db.scalars(query.order_by(ERPCatalogObservation.created_at,ERPCatalogObservation.id).limit(limit+1)))
    page=rows[:limit]
    next_cursor=None
    if len(rows)>limit:
        next_cursor=base64.urlsafe_b64encode(json.dumps(dict(q=fingerprint,t=page[-1].created_at.isoformat(),
            i=page[-1].id),separators=(',',':')).encode()).decode().rstrip('=')
    _allow(db,actor,'erp.catalog.read',source.clock(db))
    _allow(db,actor,'erp.command.read',source.clock(db))
    return CatalogObservationPage(command_id=row.id,attempt_id=attempt.id,items=[CatalogObservationView.model_validate(
        {k:getattr(item,k) for k in CatalogObservationView.model_fields}) for item in page],next_cursor=next_cursor).model_dump()


def cancel_catalog_command(db, actor, ident, body, key):
    """Explicitly terminate an UNSENT intent; a marker makes cancellation illegal.

    The current authorized canceller may replace an expired/revoked executor.
    No old source, asset blob, participant permission or session is re-opened.
    Scope fence + row locks serialize this decision with durable preparation.
    """
    from backend.app.modules.erp_catalog.erp_007_schemas import CatalogCancelInput
    from backend.app.workers.outbox import OutboxEvent
    body, key = _typed(CatalogCancelInput, body), _key(key)
    now = _guard(db, actor, 'cancel')
    row, request, frozen = _command(db, actor, ident)
    _operation(db, actor, row.operation, now)
    attempt = _attempt(db, actor, row)
    if attempt is None:
        source.fail('not_staged')
    # Bind the payload even on replay without needing the current Head version.
    wire = _wire(row, request, attempt)
    if body.input_digest != wire.input_digest:
        source.fail('cancel_conflict')
    observation_key = 'cancel:' + canonical_digest(dict(scope=[actor.organization_id, actor.store_id], key=key))
    evidence = dict(command_id=row.id, attempt_id=attempt.id, actor_id=actor.subject_id,
        expected_attempt_version=body.expected_attempt_version, input_digest=body.input_digest,
        reason_sha256=sha256(body.reason.encode()).hexdigest(), target_version=row.target_version,
        next_target_version=row.target_version + 1, effect='local_cancel_only')
    # Scope-wide lookup prevents a cancellation key being reused on another
    # command. The authority fence serializes inserts across this store.
    old = db.scalar(select(ERPCatalogObservation).where(*source.scope(ERPCatalogObservation, actor),
        ERPCatalogObservation.observation_key == observation_key, ERPCatalogObservation.kind == 'cancelled'))
    if old is not None:
        if (old.attempt_id != attempt.id or old.actor_id != actor.subject_id or old.evidence != evidence
                or old.evidence_digest != canonical_digest(evidence) or attempt.state != 'cancelled'):
            source.fail('idempotency_conflict')
        return _finish(db, actor, row, request, 'cancel', replayed=True)
    if (attempt.state != 'prepared' or attempt.dispatched_at is not None
            or any(value is not None for value in (attempt.proof, attempt.receipt_digest, attempt.applied_at,
                attempt.result_remote_version, attempt.delivery_ref, attempt.external_ref))):
        source.fail('cancel_not_unsent')
    if attempt.version != body.expected_attempt_version:
        source.fail('cancel_conflict')
    intent, approval = gateway._intent(db, actor, request), gateway._approval(db, actor, request)
    if (intent is None or approval is None or intent.id != attempt.intent_id or intent.approval_id != approval.id
            or intent.request_id != request.id or approval.request_id != request.id or approval.decision != 'approved'
            or intent.executor_id != attempt.executor_id or len({row.created_by, approval.approved_by, attempt.executor_id}) != 3
            or intent.request_digest != request.request_digest or approval.request_digest != request.request_digest
            or intent.target_digest != row.source_digest or gateway._outcome(db, actor, intent) is not None):
        source.fail('binding_changed')
    head = _busy(db, actor, row)
    if (head.busy_command_id != row.id or head.remote_version != frozen.source.sandbox_remote_version
            or head.fields != frozen.source.sandbox_fields.model_dump() or head.image_ref != frozen.source.sandbox_image_ref):
        source.fail('target_binding_invalid')
    event = db.scalar(select(OutboxEvent).where(*source.scope(OutboxEvent, actor),
        OutboxEvent.idempotency_key == 'erp7:' + attempt.id).execution_options(populate_existing=True).with_for_update())
    if (event is None or event.event_type != EVENT_TYPE + '.' + attempt.executor_id
            or event.payload != dict(attempt_id=attempt.id, input_digest=attempt.input_digest)
            or event.status not in ('pending', 'retry', 'leased', 'dead')
            or event.external_receipt is not None or event.delivered_at is not None):
        source.fail('event_invalid')
    _observation(db, actor, row, attempt, 'cancelled', observation_key, evidence, now)
    attempt.state, attempt.updated_at = 'cancelled', now
    attempt.version += 1
    head.version, head.busy_command_id, head.updated_at = head.version + 1, None, now
    # Keep identity, original payload, attempt count and all authorization rows.
    # A leased worker loses its queue lease and must recheck the Attempt before
    # it can commit a marker; its retry/ACK cannot resurrect this dead event.
    event.status, event.lease_owner, event.lease_until = 'dead', None, None
    event.last_error = 'ERP_CATALOG_CANCELLED_BEFORE_DISPATCH'
    db.flush()
    return _finish(db, actor, row, request, 'cancel')
