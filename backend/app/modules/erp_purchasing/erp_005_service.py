"""Receiving command ledger. Synthetic proof never posts a purchase or inventory.

Local functions are atomic in the caller's transaction. Only the network
entry points own their short transactions; no database lock crosses HTTP.
"""
from hashlib import sha256
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import exists, select

from backend.app.modules.audit.service import record_event
from backend.app.modules.auth.service import has_usable_account
from backend.app.modules.identity.repository import load_scope, grants_for
from backend.app.modules.identity.service import permission_allowed
from backend.app.modules.identity.command_guard import lock_scope_security_fence
from backend.app.modules.erp_commands import erp_002_service as gateway
from backend.app.modules.erp_commands.erp_002_schemas import ERPCommandPropose, ERPCommandDecide, ERPCommandStage
from backend.app.modules.erp_commands.erp_003_wire import canonical_digest
from backend.app.modules.erp_purchasing import erp_005_source as source
from backend.app.modules.erp_purchasing.erp_005_models import ERPReceiptCommand, ERPReceiptAttempt, ERPReceiptObservation
from backend.app.modules.erp_purchasing.erp_005_schemas import (
    ReceiptSourceSnapshot, ReceiptSourceContext, ReceiptProposeInput, ReceiptDecideInput, ReceiptStageInput,
)

ERPReceiptError = source.ERPReceiptError
EVENT_TYPE = 'erp.receiving.dispatch.v1'
ACTION = 'goods_receipt_document_proposal'


def _typed(model, body):
    if not isinstance(body, model):
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
    return source.guard(db, actor, 'erp.receiving.' + capability)


def _allow(db, actor, permission, now):
    source.require_person(db, actor, permission, now)


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
    row = db.scalar(select(ERPReceiptCommand).where(*source.scope(ERPReceiptCommand, actor), ERPReceiptCommand.id == ident))
    if row is None:
        source.fail('not_found')
    request = gateway._request(db, actor, row.request_id)
    try:
        frozen = ReceiptSourceSnapshot.model_validate(row.source_snapshot)
    except (ValidationError, ValueError, TypeError):
        source.fail('binding_changed')
    params = dict(route='api', source_digest=row.source_digest)
    expected = canonical_digest(dict(scope=[actor.organization_id, actor.store_id], requested_by=row.created_by,
        action=ACTION, target=['receiving_batch', row.batch_id, row.source_version, row.source_digest],
        source_sha256=row.source_digest, parameters=params, expires_at=request.expires_at.isoformat()))
    if (request.action != ACTION or request.target_kind != 'receiving_batch' or request.target_id != row.batch_id
            or request.expected_version != row.source_version or request.requested_by != row.created_by
            or request.target_digest != row.source_digest or request.evidence_digest != row.source_digest
            or request.parameters != params or request.parameters_digest != canonical_digest(params)
            or request.request_digest != expected or source.source_digest(frozen) != row.source_digest
            or (frozen.organization_id, frozen.store_id, frozen.batch_id, frozen.batch_version,
                frozen.intent.id, frozen.batch.delivery_note_sha256, row.route) != (row.organization_id, row.store_id,
                row.batch_id, row.source_version, row.purchase_intent_id, row.delivery_note_sha256, 'api')):
        source.fail('binding_changed')
    return row, request, frozen


def _attempt(db, actor, row):
    return db.scalar(select(ERPReceiptAttempt).where(*source.scope(ERPReceiptAttempt, actor),
        ERPReceiptAttempt.command_id == row.id).execution_options(populate_existing=True).with_for_update())


def _view(db, actor, row, request, *, replayed=False):
    from backend.app.modules.erp_purchasing.erp_005_schemas import ReceiptCommandView
    now = source.clock(db)
    for permission in ('erp.receiving.read', 'purchasing.read', 'receiving.read', 'erp.command.read'):
        _allow(db, actor, permission, now)
    attempt = _attempt(db, actor, row)
    details = gateway._response(db, actor, request)
    value = dict(id=row.id, batch_id=row.batch_id, purchase_intent_id=row.purchase_intent_id,
        delivery_note_sha256=row.delivery_note_sha256, route=row.route, proposal_digest=row.proposal_digest,
        created_at=row.created_at, source_version=row.source_version, source_digest=row.source_digest,
        source_snapshot=row.source_snapshot, request=details, state=attempt.state if attempt else details['state'],
        attempt=None if attempt is None else dict(id=attempt.id, intent_id=attempt.intent_id,
            executor_id=attempt.executor_id, state=attempt.state, version=attempt.version, input_digest=attempt.input_digest,
            line_results=attempt.line_results, delivery_ref=attempt.delivery_ref, external_ref=attempt.external_ref,
            receipt_digest=attempt.receipt_digest, created_at=attempt.created_at, updated_at=attempt.updated_at,
            dispatched_at=attempt.dispatched_at), mode='synthetic', effect='local_sandbox_only',
        inventory_state='not_posted', purchase_state='not_verified', replayed=replayed)
    result = ReceiptCommandView.model_validate(value).model_dump()
    for permission in ('erp.receiving.read', 'purchasing.read', 'receiving.read', 'erp.command.read'):
        _allow(db, actor, permission, source.clock(db))
    return result


def _finish(db, actor, row, request, capability, *, replayed=False):
    result = _view(db, actor, row, request, replayed=replayed)
    _allow(db, actor, 'erp.receiving.' + capability, source.clock(db))
    return result


def _audit(db, actor, operation, row, key, details, now):
    record_event(db, actor_id=actor.subject_id, organization_id=actor.organization_id, store_id=actor.store_id,
        action='erp.receiving.' + operation, target_type='erp_receipt_command', target_id=row.id,
        idempotency_key='erp5:' + canonical_digest(dict(scope=[actor.organization_id, actor.store_id],
            operation=operation, key=key)), details=details, occurred_at=now)


def read_source_context(db, actor, batch_id):
    _guard(db, actor, 'read')
    current = source.snapshot(db, actor, batch_id)
    return ReceiptSourceContext(snapshot=current, source_digest=source.source_digest(current)).model_dump()


def read_receipt_command(db, actor, ident):
    _guard(db, actor, 'read')
    row, request, _ = _command(db, actor, ident)
    return _view(db, actor, row, request)


def propose_receipt_command(db, actor, body, key):
    body, key = _typed(ReceiptProposeInput, body), _key(key)
    now = _guard(db, actor, 'propose')
    _allow(db, actor, 'erp.command.propose', now)
    digest = canonical_digest(dict(scope=[actor.organization_id, actor.store_id], actor_id=actor.subject_id,
        operation='propose', input=body.model_dump(mode='json')))
    old = db.scalar(select(ERPReceiptCommand).where(*source.scope(ERPReceiptCommand, actor), ERPReceiptCommand.proposal_key == key))
    if old:
        if old.created_by != actor.subject_id or old.proposal_digest != digest:
            source.fail('idempotency_conflict')
        row, request, _ = _command(db, actor, old.id)
        return _finish(db, actor, row, request, 'propose', replayed=True)
    current = source.snapshot(db, actor, body.batch_id)
    if current.batch_version != body.expected_batch_version or source.source_digest(current) != body.expected_source_digest:
        source.fail('source_changed')
    if db.scalar(select(ERPReceiptCommand.id).where(*source.scope(ERPReceiptCommand, actor),
            ERPReceiptCommand.batch_id == body.batch_id)):
        source.fail('batch_already_bound')
    source_hash = source.source_digest(current)
    params = dict(route='api', source_digest=source_hash)
    proposal = gateway.propose_command(db, actor, ERPCommandPropose(action=ACTION, target_id=body.batch_id,
        target_version=current.batch_version, source_sha256=source_hash, business_parameters=params,
        expires_at=body.expires_at), 'erp5:' + canonical_digest(dict(key=key,
            scope=[actor.organization_id, actor.store_id])), now=source.clock(db))
    request = gateway._request(db, actor, proposal['id'])
    row = ERPReceiptCommand(id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        batch_id=current.batch_id, purchase_intent_id=current.intent.id,
        delivery_note_sha256=current.batch.delivery_note_sha256, request_id=request.id, route='api',
        source_version=current.batch_version, source_digest=source_hash, source_snapshot=current.model_dump(mode='json'),
        proposal_key=key, proposal_digest=digest, created_by=actor.subject_id, created_at=request.created_at)
    db.add(row)
    db.flush()
    _audit(db, actor, 'proposed', row, key, dict(request_digest=request.request_digest, source_digest=source_hash,
        reason_digest=sha256(body.reason.encode()).hexdigest()), source.clock(db))
    checked, _, _ = _command(db, actor, row.id)
    return _finish(db, actor, checked, request, 'propose')


def decide_receipt_command(db, actor, ident, body, key):
    body, key = _typed(ReceiptDecideInput, body), _key(key)
    now = _guard(db, actor, 'approve')
    _allow(db, actor, 'erp.command.approve', now)
    row, request, _ = _command(db, actor, ident)
    _participant(db, actor, row.created_by, ('erp.receiving.read', 'erp.receiving.propose', 'erp.command.propose',
        'receiving.read', 'purchasing.read'), now)
    result = gateway.decide_command(db, actor, row.request_id, ERPCommandDecide(**body.model_dump()),
        'erp5:' + canonical_digest(dict(scope=[actor.organization_id, actor.store_id], key=key)), now=now)
    if not result['replayed'] and source.clock(db) >= min(request.expires_at, body.expires_at):
        source.fail('approval_expired')
    return _finish(db, actor, row, request, 'approve', replayed=result['replayed'])


def _approved_intent(db, actor, row, request, now, *, current_source=True):
    approval, intent = gateway._approval(db, actor, request), gateway._intent(db, actor, request)
    if (approval is None or intent is None or approval.decision != 'approved' or intent.approval_id != approval.id
            or intent.executor_id != actor.subject_id or len({row.created_by, approval.approved_by, intent.executor_id}) != 3
            or intent.request_digest != request.request_digest or approval.request_digest != request.request_digest
            or intent.target_digest != row.source_digest or gateway._outcome(db, actor, intent) is not None):
        source.fail('approval_invalid')
    if now >= min(request.expires_at, approval.expires_at):
        source.fail('approval_expired')
    _participant(db, actor, row.created_by, ('erp.receiving.read', 'erp.receiving.propose', 'erp.command.propose',
        'receiving.read', 'purchasing.read'), now)
    _participant(db, actor, approval.approved_by, ('erp.receiving.read', 'erp.receiving.approve', 'erp.command.approve',
        'receiving.read', 'purchasing.read'), now)
    if current_source and source.source_digest(source.snapshot(db, actor, row.batch_id)) != row.source_digest:
        source.fail('source_changed')
    return intent, approval


def _wire(row, request, attempt):
    from backend.app.modules.erp_purchasing.erp_005_wire import ReceiptDispatchRequest, dispatch_input_digest
    frozen = ReceiptSourceSnapshot.model_validate(row.source_snapshot)
    lines = [dict(line_id=line.id, line_no=number, product_id=line.product_id, product_version=line.product_version,
        intent_line_id=line.intent_line_id, quantity=line.quantity, unit=line.unit, quoted_price=line.quoted_price,
        unit_price=line.delivered_price, line_amount=line.line_total, evidence_sha256=line.evidence_sha256,
        price_evidence_sha256=line.price_evidence_sha256, final_for_intent_line=line.final_for_intent_line)
        for number, line in enumerate(frozen.batch.lines, 1)]
    value = dict(contract_id='retail.erp-receipts-sandbox', adapter_id='synthetic.receipts.v1', protocol_version=1,
        mode='synthetic', organization_id=row.organization_id, store_id=row.store_id, route='api',
        attempt_id=attempt.id, command_id=row.id, request_id=request.id, request_digest=request.request_digest,
        batch_id=row.batch_id, batch_version=row.source_version, purchase_intent_id=frozen.intent.id,
        purchase_intent_version=frozen.intent.version, delivery_note_sha256=row.delivery_note_sha256,
        source_digest=row.source_digest, currency=frozen.currency, currency_source=frozen.currency_source,
        total_amount=frozen.total_amount, cumulative=[x.model_dump() for x in frozen.cumulative], lines=lines)
    value['input_digest'] = dispatch_input_digest(value)
    if attempt.input_digest and attempt.input_digest != value['input_digest']:
        source.fail('binding_changed')
    return ReceiptDispatchRequest.model_validate(value)


def stage_receipt_command(db, actor, ident, body, key):
    body, key = _typed(ReceiptStageInput, body), _key(key)
    now = _guard(db, actor, 'dispatch')
    for permission in ('erp.command.stage', 'erp.receiving.reconcile'):
        _allow(db, actor, permission, now)
    row, request, _ = _command(db, actor, ident)
    digest = canonical_digest(dict(scope=[actor.organization_id, actor.store_id], actor_id=actor.subject_id,
        command_id=row.id, input=body.model_dump(mode='json')))
    old = db.scalar(select(ERPReceiptAttempt).where(*source.scope(ERPReceiptAttempt, actor), ERPReceiptAttempt.queue_key == key))
    if old:
        if old.command_id != row.id or old.executor_id != actor.subject_id or old.queue_digest != digest:
            source.fail('idempotency_conflict')
        return _finish(db, actor, row, request, 'dispatch', replayed=True)
    if _attempt(db, actor, row) is not None:
        source.fail('intent_already_bound')
    gateway.stage_command(db, actor, request.id, ERPCommandStage(request_digest=body.request_digest,
        approval_id=body.approval_id, expected_target_version=body.expected_batch_version),
        'erp5:' + canonical_digest(dict(scope=[actor.organization_id, actor.store_id], key=key)), now=now)
    intent, approval = _approved_intent(db, actor, row, request, source.clock(db))
    now = source.clock(db)
    expiry = min(actor.auth_expires_at, request.expires_at, approval.expires_at)
    if now >= expiry:
        source.fail('approval_expired')
    attempt = ERPReceiptAttempt(id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        command_id=row.id, intent_id=intent.id, queue_key=key, queue_digest=digest, input_digest='',
        executor_id=actor.subject_id, account_id=actor.account_id, session_id=actor.session_id,
        account_version=actor.account_version, authorization_epoch=lock_scope_security_fence(db, actor.organization_id, actor.store_id),
        authorization_expires_at=expiry, state='prepared', version=1, line_results=[], delivery_ref=None,
        external_ref=None, receipt_digest=None, created_at=now, updated_at=now, dispatched_at=None)
    wire = _wire(row, request, attempt)
    attempt.input_digest = wire.input_digest
    attempt.line_results = [dict(line_id=line.line_id, status='pending', external_line_ref=None, error_code=None) for line in wire.lines]
    db.add(attempt)
    db.flush()
    from backend.app.workers.outbox import enqueue
    enqueue(db, organization_id=actor.organization_id, store_id=actor.store_id,
        event_type=EVENT_TYPE + '.' + actor.subject_id, idempotency_key='erp5:' + attempt.id,
        payload=dict(attempt_id=attempt.id, input_digest=attempt.input_digest), now=now)
    _audit(db, actor, 'queued', row, key, dict(attempt_id=attempt.id, intent_id=intent.id, input_digest=attempt.input_digest), now)
    _approved_intent(db, actor, row, request, source.clock(db))
    return _finish(db, actor, row, request, 'dispatch')


def _observation(db, actor, row, attempt, kind, key, evidence, now):
    digest = canonical_digest(evidence)
    old = db.scalar(select(ERPReceiptObservation).where(*source.scope(ERPReceiptObservation, actor),
        ERPReceiptObservation.attempt_id == attempt.id, ERPReceiptObservation.observation_key == key))
    if old:
        if old.evidence_digest != digest or old.evidence != evidence or old.kind != kind:
            source.fail('receipt_conflict')
        return old, False
    observation = ERPReceiptObservation(id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        attempt_id=attempt.id, observation_key=key, kind=kind, actor_id=actor.subject_id,
        evidence_digest=digest, evidence=evidence, created_at=now)
    db.add(observation)
    _audit(db, actor, kind, row, attempt.id + ':' + key, dict(attempt_id=attempt.id, evidence_digest=digest,
        mode='synthetic'), now)
    db.flush()
    return observation, True


def prepare_dispatch(db, actor, ident):
    now = _guard(db, actor, 'dispatch')
    for permission in ('erp.command.stage', 'erp.receiving.reconcile'):
        _allow(db, actor, permission, now)
    row, request, _ = _command(db, actor, ident)
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
    intent, _ = _approved_intent(db, actor, row, request, now)
    if intent.id != attempt.intent_id:
        source.fail('binding_changed')
    wire = _wire(row, request, attempt)
    attempt.state, attempt.dispatched_at, attempt.updated_at = 'dispatched', now, now
    attempt.version += 1
    db.flush()
    _observation(db, actor, row, attempt, 'dispatch_intent', 'dispatch-intent',
        dict(attempt_id=attempt.id, input_digest=attempt.input_digest, route='api'), now)
    final = source.clock(db)
    for permission in ('erp.receiving.dispatch', 'erp.receiving.reconcile'):
        _allow(db, actor, permission, final)
    if final >= attempt.authorization_expires_at:
        source.fail('authorization_changed')
    _approved_intent(db, actor, row, request, final)
    return wire


def _configured(adapter, actor, route):
    from backend.app.modules.erp_purchasing.erp_005_adapter import SyntheticReceiptHTTPAdapter
    if (type(adapter) is not SyntheticReceiptHTTPAdapter or adapter.mode != 'synthetic'
            or adapter.adapter_id != 'synthetic.receipts.v1' or (adapter.organization_id, adapter.store_id, adapter.route) != (
                actor.organization_id, actor.store_id, route)):
        source.fail('adapter_unconfigured')


def _apply_ack(db, actor, ident, wire, ack):
    from backend.app.modules.erp_purchasing.erp_005_wire import ReceiptDispatchAck
    now = _guard(db, actor, 'reconcile')
    row, request, _ = _command(db, actor, ident)
    attempt = _attempt(db, actor, row)
    if attempt is None or attempt.dispatched_at is None or _wire(row, request, attempt).model_dump() != wire.model_dump():
        source.fail('binding_changed')
    ack = ReceiptDispatchAck.model_validate(ack.model_dump())
    for name in ('organization_id', 'store_id', 'route', 'attempt_id', 'command_id', 'input_digest'):
        if getattr(ack, name) != getattr(wire, name):
            source.fail('receipt_invalid')
    if attempt.delivery_ref is not None and attempt.delivery_ref != ack.delivery_ref:
        source.fail('receipt_conflict')
    evidence = ack.model_dump(exclude={'nonce', 'signature'})
    _, created = _observation(db, actor, row, attempt, 'ack', 'ack:' + canonical_digest(evidence), evidence, now)
    if created:
        attempt.delivery_ref, attempt.updated_at = ack.delivery_ref, now
        attempt.version += 1
        db.flush()
    return _finish(db, actor, row, request, 'reconcile', replayed=not created)


def _merge_readback_lines(wire, current_line_results, receipt):
    """Combine exact per-line observations without counting one external row twice."""
    from backend.app.modules.erp_purchasing.erp_005_wire import _validate_external_line_refs
    old = {line['line_id']: line for line in current_line_results}
    if len(old) != len(wire.lines) or set(old) != {line.line_id for line in wire.lines}:
        source.fail('binding_changed')
    results = []
    for number, line in enumerate(wire.lines):
        observed = receipt.lines[number] if receipt else None
        candidate = dict(line_id=line.line_id, status=observed.status if observed else 'unknown',
            external_line_ref=observed.external_line_ref if observed else None, error_code=observed.error_code if observed else None)
        if old[line.line_id]['status'] in ('accepted', 'rejected'):
            if candidate['status'] != 'unknown' and candidate != old[line.line_id]:
                source.fail('receipt_conflict')
            candidate = old[line.line_id]
        results.append(candidate)
    try:
        _validate_external_line_refs(line['external_line_ref'] for line in results)
    except ValueError:
        source.fail('receipt_conflict')
    return results


def _apply_readback(db, actor, ident, wire, receipt):
    from backend.app.modules.erp_purchasing.erp_005_wire import ReceiptReadbackReceipt, ReceiptWireLine
    now = _guard(db, actor, 'reconcile')
    row, request, _ = _command(db, actor, ident)
    attempt = _attempt(db, actor, row)
    if attempt is None or attempt.dispatched_at is None or _wire(row, request, attempt).model_dump() != wire.model_dump():
        source.fail('binding_changed')
    if receipt is not None:
        receipt = ReceiptReadbackReceipt.model_validate(receipt.model_dump())
        fields = set(type(wire).model_fields) - {'lines'}
        if any(getattr(receipt, n) != getattr(wire, n) for n in fields) or len(receipt.lines) != len(wire.lines):
            source.fail('receipt_invalid')
        for expected, observed in zip(wire.lines, receipt.lines):
            if any(getattr(expected, n) != getattr(observed, n) for n in ReceiptWireLine.model_fields):
                source.fail('receipt_invalid')
        evidence = receipt.model_dump(exclude={'nonce', 'signature'})
    else:
        evidence = dict(attempt_id=attempt.id, input_digest=attempt.input_digest, result='not_found_unknown')
    results = _merge_readback_lines(wire, attempt.line_results, receipt)
    if attempt.state == 'sandbox_verified' and receipt is not None:
        if receipt.document_state == 'rejected' or (receipt.external_ref is not None and receipt.external_ref != attempt.external_ref):
            source.fail('receipt_conflict')
    # The existing reconcile authority guard holds the store lock through
    # commit. Test only the new accepted binding, and keep the unique backstop.
    if receipt is not None and receipt.document_state == 'accepted' and attempt.external_ref is None:
        claimed = select(exists().where(*source.scope(ERPReceiptAttempt, actor),
            ERPReceiptAttempt.external_ref == receipt.external_ref, ERPReceiptAttempt.id != attempt.id))
        if db.scalar(claimed):
            source.fail('receipt_conflict')
    _, created = _observation(db, actor, row, attempt, 'readback', 'readback:' + canonical_digest(evidence), evidence, now)
    if created:
        attempt.line_results = results
        if receipt and receipt.document_state == 'accepted':
            if attempt.external_ref is not None and attempt.external_ref != receipt.external_ref:
                source.fail('receipt_conflict')
            attempt.state, attempt.external_ref, attempt.receipt_digest = 'sandbox_verified', receipt.external_ref, canonical_digest(evidence)
        elif attempt.state != 'sandbox_verified':
            accepted = sum(line['status'] == 'accepted' for line in results)
            rejected = sum(line['status'] == 'rejected' for line in results)
            attempt.state = 'partial' if accepted else 'failed' if rejected == len(results) else 'unknown'
        attempt.updated_at = now
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
        evidence = dict(code=error.code, phase=error.phase, maybe_sent=error.maybe_sent)
        _, created = _observation(db, actor, row, attempt, 'network_error', 'error:' + canonical_digest(evidence), evidence, now)
        if created and attempt.state not in ('sandbox_verified', 'failed', 'partial'):
            attempt.state, attempt.updated_at = 'unknown', now
            attempt.version += 1
            db.flush()
        return _finish(db, actor, row, request, 'reconcile', replayed=not created)


def dispatch_receipt_command(factory, actor, ident, adapter=None, *, enabled=False):
    from backend.app.modules.erp_commands.erp_003_wire import SandboxAdapterError
    if type(enabled) is not bool:
        source.fail('input_invalid')
    if not enabled:
        source.fail('dispatch_disabled')
    with factory.begin() as db:
        _guard(db, actor, 'dispatch')
        row, _, _ = _command(db, actor, ident)
        _configured(adapter, actor, row.route)
        wire = prepare_dispatch(db, actor, ident)
        if wire is None:
            return _finish(db, actor, row, gateway._request(db, actor, row.request_id), 'dispatch', replayed=True)
    try:
        ack = adapter.dispatch(wire)
    except SandboxAdapterError as error:
        return _network_error(factory, actor, ident, error)
    with factory.begin() as db:
        _apply_ack(db, actor, ident, wire, ack)
    return reconcile_receipt_command(factory, actor, ident, adapter)


def reconcile_receipt_command(factory, actor, ident, adapter=None):
    from backend.app.modules.erp_commands.erp_003_wire import SandboxAdapterError
    with factory.begin() as db:
        _guard(db, actor, 'reconcile')
        row, request, _ = _command(db, actor, ident)
        _configured(adapter, actor, row.route)
        attempt = _attempt(db, actor, row)
        if attempt is None or attempt.dispatched_at is None:
            source.fail('not_dispatched')
        wire = _wire(row, request, attempt)
    try:
        receipt = adapter.readback(wire)
    except SandboxAdapterError as error:
        return _network_error(factory, actor, ident, error)
    with factory.begin() as db:
        return _apply_readback(db, actor, ident, wire, receipt)


def command_status(db,actor,key,proposal_digest):
    import re
    from backend.app.modules.erp_purchasing.erp_005_schemas import ReceiptCommandStatus
    key=_key(key)
    if type(proposal_digest) is not str or re.fullmatch(r'[0-9a-f]{64}',proposal_digest) is None:
        source.fail('input_invalid')
    _guard(db,actor,'read')
    _allow(db,actor,'erp.command.read',source.clock(db))
    row=db.scalar(select(ERPReceiptCommand).where(*source.scope(ERPReceiptCommand,actor),ERPReceiptCommand.proposal_key==key))
    if row is None:
        return ReceiptCommandStatus(status='not_found',command=None).model_dump()
    if row.created_by!=actor.subject_id or row.proposal_digest!=proposal_digest:
        source.fail('not_found')
    row,request,_=_command(db,actor,row.id)
    return ReceiptCommandStatus(status='found',command=_view(db,actor,row,request,replayed=True)).model_dump()


def read_observations(db,actor,ident,*,limit=50,cursor=None):
    import base64
    import json
    from datetime import datetime
    from pydantic import TypeAdapter
    from sqlalchemy import and_,or_
    from backend.app.modules.erp_purchasing.erp_005_schemas import (
        Identifier,StrictCursor,ReceiptObservationView,ReceiptObservationPage,
    )
    if type(limit) is not int or not 1<=limit<=100:
        source.fail('input_invalid')
    _guard(db,actor,'read')
    _allow(db,actor,'erp.command.read',source.clock(db))
    row,request,_=_command(db,actor,ident)
    attempt=_attempt(db,actor,row)
    if attempt is None:
        if cursor is not None:
            source.fail('cursor_invalid')
        return ReceiptObservationPage(command_id=row.id,attempt_id=None,items=[],next_cursor=None).model_dump()
    fingerprint=canonical_digest(dict(scope=[actor.organization_id,actor.store_id],actor=actor.subject_id,
        command_id=row.id,attempt_id=attempt.id))
    query=select(ERPReceiptObservation).where(*source.scope(ERPReceiptObservation,actor),
        ERPReceiptObservation.attempt_id==attempt.id)
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
        query=query.where(or_(ERPReceiptObservation.created_at>at,and_(ERPReceiptObservation.created_at==at,
            ERPReceiptObservation.id>item_id)))
    rows=list(db.scalars(query.order_by(ERPReceiptObservation.created_at,ERPReceiptObservation.id).limit(limit+1)))
    page=rows[:limit]
    next_cursor=None
    if len(rows)>limit:
        next_cursor=base64.urlsafe_b64encode(json.dumps(dict(q=fingerprint,t=page[-1].created_at.isoformat(),
            i=page[-1].id),separators=(',',':')).encode()).decode().rstrip('=')
    _allow(db,actor,'erp.receiving.read',source.clock(db))
    _allow(db,actor,'erp.command.read',source.clock(db))
    return ReceiptObservationPage(command_id=row.id,attempt_id=attempt.id,items=[ReceiptObservationView.model_validate(
        {k:getattr(item,k) for k in ReceiptObservationView.model_fields}) for item in page],next_cursor=next_cursor).model_dump()
