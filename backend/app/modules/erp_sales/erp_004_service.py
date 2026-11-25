"""ERP-004 real-person sales command binding. Caller owns local transactions.

Synthetic delivery/document proof remains separate from formal ERP outcomes and
settlement. There is no network effect in proposal, approval, staging or reads.
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
from backend.app.modules.erp_commands.erp_002_schemas import ERPCommandPropose,ERPCommandDecide,ERPCommandStage
from backend.app.modules.sales import service as sales
from backend.app.modules.sales.schemas import HandoffCreate
from backend.app.modules.erp_sales import erp_004_source as source
from backend.app.modules.erp_sales.erp_004_models import ERPSalesCommand,ERPSalesAttempt,ERPSalesObservation
from backend.app.modules.erp_sales.erp_004_schemas import SalesSourceContext,SalesSourceSnapshot,SalesProposeInput,SalesDecideInput,SalesStageInput,SalesCommandView
from backend.app.modules.erp_commands.erp_003_wire import canonical_digest

ERPSalesError=source.ERPSalesError
EVENT_TYPE='erp.sales.dispatch.v1'


def _typed(model,body):
    if not isinstance(body,model):
        source.fail('input_invalid')
    try:
        return model.model_validate(body.model_dump(warnings=False))
    except (ValidationError,ValueError,TypeError):
        source.fail('input_invalid')


def _key(value):
    if type(value) is not str or not 1<=len(value)<=200 or value!=value.strip() or any(ord(c)<32 for c in value):
        source.fail('key_invalid')
    return value


def _guard(db,actor,capability):
    now=source.guard(db,actor,'erp.sales.'+capability)
    source.require_person(db,actor,'erp.sales.read',now)
    source.require_person(db,actor,'erp.command.read',now)
    return now


def _allow(db,actor,permission,now):
    source.require_person(db,actor,permission,now)


def _participant(db,actor,ident,permissions,now):
    org,store,subject,roles=load_scope(db,actor.organization_id,actor.store_id,ident)
    if (not org or not org.active or not store or not store.active or store.organization_id!=actor.organization_id
            or not subject or not subject.active or subject.organization_id!=actor.organization_id
            or subject.subject_type!='person' or not has_usable_account(db,organization_id=actor.organization_id,
                store_id=actor.store_id,subject_id=ident,now=now)):
        source.fail('participant_unavailable')
    for permission in permissions:
        if not permission_allowed(grants_for(db,actor.organization_id,actor.store_id,ident,roles,permission),ident,now=now):
            source.fail('participant_unavailable')


def _command(db,actor,ident):
    row=db.scalar(select(ERPSalesCommand).where(*source.scope(ERPSalesCommand,actor),ERPSalesCommand.id==ident))
    if row is None:
        source.fail('not_found')
    request=gateway._request(db,actor,row.request_id)
    try:
        frozen=SalesSourceSnapshot.model_validate(row.source_snapshot)
    except (ValidationError,ValueError,TypeError):
        source.fail('binding_changed')
    params=dict(route=row.route,source_digest=row.source_digest,handoff_id=row.handoff_id)
    expected=canonical_digest(dict(scope=[actor.organization_id,actor.store_id],requested_by=row.created_by,
        action='sales_document_proposal',target=['sales_draft',row.draft_id,row.source_version,row.source_digest],
        source_sha256=row.source_digest,parameters=params,expires_at=request.expires_at.isoformat()))
    if (request.action!='sales_document_proposal' or request.target_kind!='sales_draft'
            or request.target_id!=row.draft_id or request.expected_version!=row.source_version
            or request.requested_by!=row.created_by or request.target_digest!=row.source_digest
            or request.evidence_digest!=row.source_digest or request.parameters!=params
            or request.parameters_digest!=canonical_digest(params) or request.request_digest!=expected
            or source.source_digest(frozen)!=row.source_digest
            or (frozen.organization_id,frozen.store_id,frozen.draft_id,frozen.draft_version,frozen.handoff_id)!=(
                row.organization_id,row.store_id,row.draft_id,row.source_version,row.handoff_id)):
        source.fail('binding_changed')
    return row,request,frozen


def _attempt(db,actor,command):
    return db.scalar(select(ERPSalesAttempt).where(*source.scope(ERPSalesAttempt,actor),
        ERPSalesAttempt.command_id==command.id).execution_options(populate_existing=True).with_for_update())


def _view(db,actor,row,request,*,replayed=False):
    now=source.clock(db)
    _allow(db,actor,'erp.sales.read',now)
    _allow(db,actor,'sales.read',now)
    _allow(db,actor,'erp.command.read',now)
    attempt=_attempt(db,actor,row)
    details=gateway._response(db,actor,request)
    value=dict(id=row.id,draft_id=row.draft_id,handoff_id=row.handoff_id,route=row.route,
        proposal_digest=row.proposal_digest,created_at=row.created_at,source_version=row.source_version,source_digest=row.source_digest,source_snapshot=row.source_snapshot,
        request=details,state=attempt.state if attempt else details['state'],attempt=None if attempt is None else dict(
            id=attempt.id,intent_id=attempt.intent_id,executor_id=attempt.executor_id,state=attempt.state,
            version=attempt.version,input_digest=attempt.input_digest,line_results=attempt.line_results,
            delivery_ref=attempt.delivery_ref,external_ref=attempt.external_ref,receipt_digest=attempt.receipt_digest,
            created_at=attempt.created_at,updated_at=attempt.updated_at,dispatched_at=attempt.dispatched_at),
        mode='synthetic',effect='local_sandbox_only',payment_state='not_verified',replayed=replayed)
    result=SalesCommandView.model_validate(value).model_dump()
    final=source.clock(db)
    for permission in ('erp.sales.read','sales.read','erp.command.read'):
        _allow(db,actor,permission,final)
    return result


def _audit(db,actor,operation,row,key,details,now):
    record_event(db,actor_id=actor.subject_id,organization_id=actor.organization_id,store_id=actor.store_id,
        action='erp.sales.'+operation,target_type='erp_sales_command',target_id=row.id,
        idempotency_key='ers4:'+canonical_digest(dict(scope=[actor.organization_id,actor.store_id],operation=operation,key=key)),
        details=details,occurred_at=now)


def _finish(db,actor,row,request,capability,*,replayed=False):
    result=_view(db,actor,row,request,replayed=replayed)
    _allow(db,actor,'erp.sales.'+capability,source.clock(db))
    return result


def read_source_context(db,actor,draft_id):
    _guard(db,actor,'read')
    current=source.snapshot(db,actor,draft_id)
    result=SalesSourceContext(snapshot=current,source_digest=source.source_digest(current)).model_dump()
    _allow(db,actor,'erp.sales.read',source.clock(db))
    return result


def read_sales_command(db,actor,ident):
    _guard(db,actor,'read')
    row,request,_=_command(db,actor,ident)
    return _view(db,actor,row,request)


def propose_sales_command(db,actor,body,key):
    body,key=_typed(SalesProposeInput,body),_key(key)
    now=_guard(db,actor,'propose')
    _allow(db,actor,'erp.command.propose',now)
    _allow(db,actor,'sales.handoff',now)
    request_hash=canonical_digest(dict(scope=[actor.organization_id,actor.store_id],actor_id=actor.subject_id,
        operation='propose',input=body.model_dump(mode='json')))
    old=db.scalar(select(ERPSalesCommand).where(*source.scope(ERPSalesCommand,actor),ERPSalesCommand.proposal_key==key))
    if old:
        if old.created_by!=actor.subject_id or old.proposal_digest!=request_hash:
            source.fail('idempotency_conflict')
        row,request,_=_command(db,actor,old.id)
        return _finish(db,actor,row,request,'propose',replayed=True)
    current=source.snapshot(db,actor,body.draft_id)
    if current.draft_version!=body.expected_draft_version or source.source_digest(current)!=body.expected_source_digest:
        source.fail('source_changed')
    if current.handoff_id is not None:
        source.fail('route_already_bound')
    if db.scalar(select(ERPSalesCommand.id).where(*source.scope(ERPSalesCommand,actor),
            ERPSalesCommand.draft_id==body.draft_id)):
        source.fail('route_already_bound')
    # Shared legacy unique(draft) reservation is made before a new ERP request.
    # All writes remain inside the caller's single transaction.
    handoff=sales.create_handoff(db,actor,body.draft_id,HandoffCreate(),
        idempotency_key='ers4:'+canonical_digest(dict(key=key,scope=[actor.organization_id,actor.store_id])),now=source.clock(db))
    db.flush()
    frozen=source.snapshot(db,actor,body.draft_id,require_handoff=True)
    source_hash=source.source_digest(frozen)
    params=dict(route=body.route,source_digest=source_hash,handoff_id=handoff.id)
    proposal=gateway.propose_command(db,actor,ERPCommandPropose(action='sales_document_proposal',
        target_id=body.draft_id,target_version=frozen.draft_version,source_sha256=source_hash,
        business_parameters=params,expires_at=body.expires_at),
        'ers4:'+canonical_digest(dict(key=key,scope=[actor.organization_id,actor.store_id])),now=source.clock(db))
    request=gateway._request(db,actor,proposal['id'])
    row=ERPSalesCommand(id=str(uuid4()),organization_id=actor.organization_id,store_id=actor.store_id,
        draft_id=body.draft_id,handoff_id=handoff.id,request_id=request.id,route=body.route,
        source_version=frozen.draft_version,source_digest=source_hash,source_snapshot=frozen.model_dump(mode='json'),
        proposal_key=key,proposal_digest=request_hash,created_by=actor.subject_id,created_at=request.created_at)
    db.add(row);db.flush()
    _audit(db,actor,'proposed',row,key,dict(request_digest=request.request_digest,source_digest=source_hash,
        route=row.route,reason_digest=sha256(body.reason.encode()).hexdigest()),source.clock(db))
    checked,_,_=_command(db,actor,row.id)
    return _finish(db,actor,checked,request,'propose')


def finalize_proposal(db, actor, ident, now, *, new_effect):
    """Owner's read-only commit check; preserve known replay preconditions."""
    for permission in ('erp.sales.propose', 'erp.command.propose', 'sales.handoff',
                       'erp.sales.read', 'erp.command.read', 'sales.read'):
        _allow(db, actor, permission, now)
    if new_effect:
        row, request, _ = _command(db, actor, ident)
        if request.expires_at <= now:
            source.fail('approval_expired')
        draft = sales._draft(db, actor, row.draft_id)
        sales.validate_draft_quotes(db, actor, draft, now)


def decide_sales_command(db,actor,ident,body,key):
    body,key=_typed(SalesDecideInput,body),_key(key)
    now=_guard(db,actor,'approve')
    _allow(db,actor,'erp.command.approve',now)
    row,request,_=_command(db,actor,ident)
    _participant(db,actor,row.created_by,('erp.sales.read','erp.sales.propose','erp.command.propose','sales.read'),now)
    result=gateway.decide_command(db,actor,row.request_id,ERPCommandDecide(**body.model_dump()),
        'ers4:'+canonical_digest(dict(scope=[actor.organization_id,actor.store_id],key=key)),now=now)
    if not result['replayed'] and source.clock(db)>=min(request.expires_at,body.expires_at):
        source.fail('approval_expired')
    return _finish(db,actor,row,request,'approve',replayed=result['replayed'])


def _approved_intent(db,actor,row,request,now,*,current_source=True):
    approval=gateway._approval(db,actor,request)
    intent=gateway._intent(db,actor,request)
    if (approval is None or intent is None or approval.decision!='approved' or intent.approval_id!=approval.id
            or intent.executor_id!=actor.subject_id or len({row.created_by,approval.approved_by,intent.executor_id})!=3
            or intent.request_digest!=request.request_digest or approval.request_digest!=request.request_digest
            or intent.target_digest!=row.source_digest or gateway._outcome(db,actor,intent) is not None):
        source.fail('approval_invalid')
    if now>=min(request.expires_at,approval.expires_at):
        source.fail('approval_expired')
    _participant(db,actor,row.created_by,('erp.sales.read','erp.sales.propose','erp.command.propose','sales.read'),now)
    _participant(db,actor,approval.approved_by,('erp.sales.read','erp.sales.approve','erp.command.approve','sales.read'),now)
    if current_source:
        current=source.snapshot(db,actor,row.draft_id,require_handoff=True)
        if source.source_digest(current)!=row.source_digest:
            source.fail('source_changed')
    return intent,approval


def _wire(row,request,attempt):
    # Lazy import while the bounded wire package is being implemented. A
    # missing package is an error, never a permissive dispatch fallback.
    from backend.app.modules.erp_sales.erp_004_wire import SalesDispatchRequest,dispatch_input_digest
    frozen=SalesSourceSnapshot.model_validate(row.source_snapshot)
    lines=[dict(line_id=line.id,line_no=number,product_id=line.product_id,quantity=line.quantity,
        unit_price=line.unit_price,discount_bps=line.discount_bps,discounted_unit_price=line.discounted_unit_price,
        line_amount=line.line_amount) for number,line in enumerate(frozen.lines,1)]
    value=dict(contract_id='retail.erp-sales-sandbox',adapter_id='synthetic.sales.v1',protocol_version=1,mode='synthetic',
        organization_id=row.organization_id,store_id=row.store_id,route=row.route,attempt_id=attempt.id,
        command_id=row.id,request_id=request.id,request_digest=request.request_digest,draft_id=row.draft_id,
        draft_version=row.source_version,handoff_id=row.handoff_id,source_digest=row.source_digest,
        currency=frozen.currency,total_amount=frozen.total_amount,lines=lines)
    value['input_digest']=dispatch_input_digest(value)
    if attempt.input_digest and attempt.input_digest!=value['input_digest']:
        source.fail('binding_changed')
    return SalesDispatchRequest.model_validate(value)


def stage_sales_command(db,actor,ident,body,key):
    body,key=_typed(SalesStageInput,body),_key(key)
    now=_guard(db,actor,'dispatch')
    for permission in ('erp.command.stage','erp.sales.reconcile'):
        _allow(db,actor,permission,now)
    row,request,_=_command(db,actor,ident)
    digest=canonical_digest(dict(scope=[actor.organization_id,actor.store_id],actor_id=actor.subject_id,
        command_id=row.id,input=body.model_dump(mode='json')))
    old=db.scalar(select(ERPSalesAttempt).where(*source.scope(ERPSalesAttempt,actor),ERPSalesAttempt.queue_key==key))
    if old:
        if old.command_id!=row.id or old.executor_id!=actor.subject_id or old.queue_digest!=digest:
            source.fail('idempotency_conflict')
        return _finish(db,actor,row,request,'dispatch',replayed=True)
    if _attempt(db,actor,row) is not None:
        source.fail('intent_already_bound')
    gateway.stage_command(db,actor,request.id,ERPCommandStage(request_digest=body.request_digest,
        approval_id=body.approval_id,expected_target_version=body.expected_draft_version),
        'ers4:'+canonical_digest(dict(scope=[actor.organization_id,actor.store_id],key=key)),now=now)
    intent,approval=_approved_intent(db,actor,row,request,source.clock(db))
    now=source.clock(db)
    expiry=min(actor.auth_expires_at,request.expires_at,approval.expires_at)
    if now>=expiry:
        source.fail('approval_expired')
    attempt=ERPSalesAttempt(id=str(uuid4()),organization_id=actor.organization_id,store_id=actor.store_id,
        command_id=row.id,intent_id=intent.id,queue_key=key,queue_digest=digest,input_digest='',
        executor_id=actor.subject_id,account_id=actor.account_id,session_id=actor.session_id,
        account_version=actor.account_version,authorization_epoch=lock_scope_security_fence(db,actor.organization_id,actor.store_id),
        authorization_expires_at=expiry,state='prepared',version=1,line_results=[],delivery_ref=None,external_ref=None,
        receipt_digest=None,created_at=now,updated_at=now,dispatched_at=None)
    wire=_wire(row,request,attempt)
    attempt.input_digest=wire.input_digest
    attempt.line_results=[dict(line_id=line.line_id,status='pending',external_line_ref=None,error_code=None) for line in wire.lines]
    db.add(attempt);db.flush()
    from backend.app.workers.outbox import enqueue
    enqueue(db,organization_id=actor.organization_id,store_id=actor.store_id,event_type=EVENT_TYPE+'.'+actor.subject_id,
        idempotency_key='ers4:'+attempt.id,payload=dict(attempt_id=attempt.id,input_digest=attempt.input_digest),now=now)
    _audit(db,actor,'queued',row,key,dict(attempt_id=attempt.id,intent_id=intent.id,input_digest=attempt.input_digest),now)
    _approved_intent(db,actor,row,request,source.clock(db))
    return _finish(db,actor,row,request,'dispatch')


def _observation(db,actor,row,attempt,kind,key,evidence,now):
    value=canonical_digest(evidence)
    old=db.scalar(select(ERPSalesObservation).where(*source.scope(ERPSalesObservation,actor),
        ERPSalesObservation.attempt_id==attempt.id,ERPSalesObservation.observation_key==key))
    if old is not None:
        if old.evidence_digest!=value or old.evidence!=evidence or old.kind!=kind:
            source.fail('receipt_conflict')
        return old,False
    observed=ERPSalesObservation(id=str(uuid4()),organization_id=actor.organization_id,store_id=actor.store_id,
        attempt_id=attempt.id,observation_key=key,kind=kind,actor_id=actor.subject_id,evidence_digest=value,
        evidence=evidence,created_at=now)
    db.add(observed)
    _audit(db,actor,kind,row,attempt.id+':'+key,dict(attempt_id=attempt.id,evidence_digest=value,
        mode='synthetic'),now)
    db.flush()
    return observed,True


def prepare_dispatch(db,actor,command_id):
    now=_guard(db,actor,'dispatch')
    for permission in ('erp.command.stage','erp.sales.reconcile'):
        _allow(db,actor,permission,now)
    row,request,_=_command(db,actor,command_id)
    attempt=_attempt(db,actor,row)
    if attempt is None:
        source.fail('not_staged')
    if attempt.dispatched_at is not None:
        return None
    if attempt.state!='prepared':
        source.fail('attempt_state_invalid')
    if (attempt.executor_id,attempt.account_id,attempt.session_id,attempt.account_version)!=(
            actor.subject_id,actor.account_id,actor.session_id,actor.account_version):
        source.fail('original_executor_required')
    if (attempt.authorization_epoch!=lock_scope_security_fence(db,actor.organization_id,actor.store_id)
            or now>=attempt.authorization_expires_at):
        source.fail('authorization_changed')
    intent,_=_approved_intent(db,actor,row,request,now)
    if intent.id!=attempt.intent_id:
        source.fail('binding_changed')
    wire=_wire(row,request,attempt)
    # Commit this intent BEFORE the HTTP POST. A crash or any network error
    # keeps dispatched_at forever and can only be reconciled with GET.
    attempt.state,attempt.dispatched_at,attempt.updated_at='dispatched',now,now
    attempt.version+=1
    db.flush()
    _observation(db,actor,row,attempt,'dispatch_intent','dispatch-intent',
        dict(attempt_id=attempt.id,input_digest=wire.input_digest,route=row.route),now)
    final=source.clock(db)
    _allow(db,actor,'erp.sales.dispatch',final)
    _allow(db,actor,'erp.sales.reconcile',final)
    if final>=attempt.authorization_expires_at:
        source.fail('authorization_changed')
    _approved_intent(db,actor,row,request,final)
    return wire


def _configured(adapter,actor,row):
    from backend.app.modules.erp_sales.erp_004_adapter import SyntheticSalesHTTPAdapter
    if (type(adapter) is not SyntheticSalesHTTPAdapter or adapter.mode!='synthetic'
            or adapter.adapter_id!='synthetic.sales.v1'
            or (adapter.organization_id,adapter.store_id,adapter.route)!=(actor.organization_id,actor.store_id,row.route)):
        source.fail('adapter_unconfigured')


def _apply_ack(db,actor,command_id,wire,ack):
    from backend.app.modules.erp_sales.erp_004_wire import SalesDispatchAck
    now=_guard(db,actor,'reconcile')
    row,request,_=_command(db,actor,command_id)
    attempt=_attempt(db,actor,row)
    if attempt is None or attempt.dispatched_at is None or _wire(row,request,attempt).model_dump()!=wire.model_dump():
        source.fail('binding_changed')
    ack=SalesDispatchAck.model_validate(ack.model_dump())
    for name in ('organization_id','store_id','route','attempt_id','command_id','input_digest'):
        if getattr(ack,name)!=getattr(wire,name):
            source.fail('receipt_invalid')
    if attempt.delivery_ref is not None and attempt.delivery_ref!=ack.delivery_ref:
        source.fail('receipt_conflict')
    evidence=ack.model_dump(exclude={'nonce','signature'})
    _,created=_observation(db,actor,row,attempt,'ack','ack:'+canonical_digest(evidence),evidence,now)
    if created:
        attempt.delivery_ref=ack.delivery_ref
        attempt.updated_at=now
        attempt.version+=1
        db.flush()
    # The reserved legacy Handoff remains requested; a synthetic ACK must not
    # update the official/source sales facts or simulate a formal receipt.
    _allow(db,actor,'erp.sales.reconcile',source.clock(db))
    return _view(db,actor,row,request,replayed=not created)


def _apply_readback(db,actor,command_id,wire,receipt):
    from backend.app.modules.erp_sales.erp_004_wire import SalesReadbackReceipt,SalesWireLine
    now=_guard(db,actor,'reconcile')
    row,request,_=_command(db,actor,command_id)
    attempt=_attempt(db,actor,row)
    if attempt is None or attempt.dispatched_at is None or _wire(row,request,attempt).model_dump()!=wire.model_dump():
        source.fail('binding_changed')
    if receipt is not None:
        receipt=SalesReadbackReceipt.model_validate(receipt.model_dump())
        names=('contract_id','adapter_id','protocol_version','mode','organization_id','store_id','route',
            'attempt_id','command_id','request_id','request_digest','draft_id','draft_version','handoff_id',
            'source_digest','input_digest','currency','total_amount')
        if any(getattr(receipt,n)!=getattr(wire,n) for n in names) or len(receipt.lines)!=len(wire.lines):
            source.fail('receipt_invalid')
        for expected,observed in zip(wire.lines,receipt.lines):
            if any(getattr(expected,n)!=getattr(observed,n) for n in SalesWireLine.model_fields):
                source.fail('receipt_invalid')
        evidence=receipt.model_dump(exclude={'nonce','signature'})
    else:
        evidence=dict(attempt_id=attempt.id,input_digest=attempt.input_digest,result='not_found_unknown')
    # Compare every old verified line before recording anything. A new unknown
    # result never erases already known evidence, and conflicting facts reject.
    old_results={line['line_id']:line for line in attempt.line_results}
    if (len(old_results)!=len(wire.lines) or set(old_results)!={line.line_id for line in wire.lines}):
        source.fail('binding_changed')
    result=[]
    for number,line in enumerate(wire.lines):
        old=old_results[line.line_id]
        observed=receipt.lines[number] if receipt else None
        candidate=dict(line_id=line.line_id,status=observed.status if observed else 'unknown',
            external_line_ref=observed.external_line_ref if observed else None,
            error_code=observed.error_code if observed else None)
        if old['status'] in ('accepted','rejected'):
            if candidate['status']!='unknown' and candidate!=old:
                source.fail('receipt_conflict')
            candidate=old
        result.append(candidate)
    if attempt.state=='sandbox_verified' and receipt is not None:
        if (receipt.document_state=='rejected' or (receipt.external_ref is not None
                and receipt.external_ref!=attempt.external_ref)):
            source.fail('receipt_conflict')
    _,created=_observation(db,actor,row,attempt,'readback','readback:'+canonical_digest(evidence),evidence,now)
    if created:
        attempt.line_results=result
        if receipt and receipt.document_state=='accepted':
            if attempt.external_ref is not None and attempt.external_ref!=receipt.external_ref:
                source.fail('receipt_conflict')
            attempt.state='sandbox_verified'
            attempt.external_ref=receipt.external_ref
            attempt.receipt_digest=canonical_digest(evidence)
        elif attempt.state!='sandbox_verified':
            accepted=sum(line['status']=='accepted' for line in result)
            rejected=sum(line['status']=='rejected' for line in result)
            attempt.state='partial' if accepted else 'failed' if rejected==len(result) else 'unknown'
        attempt.updated_at=now
        attempt.version+=1
        db.flush()
    _allow(db,actor,'erp.sales.reconcile',source.clock(db))
    return _view(db,actor,row,request,replayed=not created)


def _record_network_error(factory,actor,command_id,error):
    with factory.begin() as db:
        now=_guard(db,actor,'reconcile')
        row,request,_=_command(db,actor,command_id)
        attempt=_attempt(db,actor,row)
        if attempt is None or attempt.dispatched_at is None:
            source.fail('not_dispatched')
        evidence=dict(code=error.code,phase=error.phase,maybe_sent=error.maybe_sent)
        _,created=_observation(db,actor,row,attempt,'network_error','error:'+canonical_digest(evidence),evidence,now)
        if created and attempt.state not in ('sandbox_verified','failed','partial'):
            attempt.state='unknown'
            attempt.updated_at=now
            attempt.version+=1
            db.flush()
        return _finish(db,actor,row,request,'reconcile',replayed=not created)


def dispatch_sales_command(factory,actor,command_id,adapter=None,*,enabled=False):
    from backend.app.modules.erp_commands.erp_003_wire import SandboxAdapterError
    if type(enabled) is not bool:
        source.fail('input_invalid')
    if not enabled:
        source.fail('dispatch_disabled')
    with factory.begin() as db:
        _guard(db,actor,'dispatch')
        row,request,_=_command(db,actor,command_id)
        _configured(adapter,actor,row)
        wire=prepare_dispatch(db,actor,command_id)
        if wire is None:
            return _finish(db,actor,row,request,'dispatch',replayed=True)
    try:
        ack=adapter.dispatch(wire)
    except SandboxAdapterError as error:
        return _record_network_error(factory,actor,command_id,error)
    with factory.begin() as db:
        _apply_ack(db,actor,command_id,wire,ack)
    return reconcile_sales_command(factory,actor,command_id,adapter)


def reconcile_sales_command(factory,actor,command_id,adapter=None):
    from backend.app.modules.erp_commands.erp_003_wire import SandboxAdapterError
    with factory.begin() as db:
        _guard(db,actor,'reconcile')
        row,request,_=_command(db,actor,command_id)
        _configured(adapter,actor,row)
        attempt=_attempt(db,actor,row)
        if attempt is None or attempt.dispatched_at is None:
            source.fail('not_dispatched')
        wire=_wire(row,request,attempt)
    # Current authorized people can GET the same committed intent even after
    # the original executor session/approval expired; this cannot authorize POST.
    try:
        receipt=adapter.readback(wire)
    except SandboxAdapterError as error:
        return _record_network_error(factory,actor,command_id,error)
    with factory.begin() as db:
        return _apply_readback(db,actor,command_id,wire,receipt)


def command_status(db,actor,key,proposal_digest):
    import re
    from backend.app.modules.erp_sales.erp_004_schemas import SalesCommandStatus
    key=_key(key)
    if type(proposal_digest) is not str or re.fullmatch(r'[0-9a-f]{64}',proposal_digest) is None:
        source.fail('input_invalid')
    _guard(db,actor,'read')
    _allow(db,actor,'erp.command.read',source.clock(db))
    row=db.scalar(select(ERPSalesCommand).where(*source.scope(ERPSalesCommand,actor),ERPSalesCommand.proposal_key==key))
    if row is None:
        return SalesCommandStatus(status='not_found',command=None).model_dump()
    if row.created_by!=actor.subject_id or row.proposal_digest!=proposal_digest:
        source.fail('not_found')
    row,request,_=_command(db,actor,row.id)
    return SalesCommandStatus(status='found',command=_view(db,actor,row,request,replayed=True)).model_dump()


def read_observations(db,actor,ident,*,limit=50,cursor=None):
    import base64
    import json
    from datetime import datetime
    from pydantic import TypeAdapter
    from sqlalchemy import and_,or_
    from backend.app.modules.erp_sales.erp_004_schemas import (
        Identifier,StrictCursor,SalesObservationView,SalesObservationPage,
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
        return SalesObservationPage(command_id=row.id,attempt_id=None,items=[],next_cursor=None).model_dump()
    fingerprint=canonical_digest(dict(scope=[actor.organization_id,actor.store_id],actor=actor.subject_id,
        command_id=row.id,attempt_id=attempt.id))
    query=select(ERPSalesObservation).where(*source.scope(ERPSalesObservation,actor),
        ERPSalesObservation.attempt_id==attempt.id)
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
        query=query.where(or_(ERPSalesObservation.created_at>at,and_(ERPSalesObservation.created_at==at,
            ERPSalesObservation.id>item_id)))
    rows=list(db.scalars(query.order_by(ERPSalesObservation.created_at,ERPSalesObservation.id).limit(limit+1)))
    page=rows[:limit]
    next_cursor=None
    if len(rows)>limit:
        next_cursor=base64.urlsafe_b64encode(json.dumps(dict(q=fingerprint,t=page[-1].created_at.isoformat(),
            i=page[-1].id),separators=(',',':')).encode()).decode().rstrip('=')
    _allow(db,actor,'erp.sales.read',source.clock(db))
    _allow(db,actor,'erp.command.read',source.clock(db))
    return SalesObservationPage(command_id=row.id,attempt_id=attempt.id,items=[SalesObservationView.model_validate(
        {k:getattr(item,k) for k in SalesObservationView.model_fields}) for item in page],next_cursor=next_cursor).model_dump()
