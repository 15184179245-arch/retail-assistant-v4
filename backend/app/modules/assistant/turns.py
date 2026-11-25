"""Immutable same-Task followups; only explicit Host policy creates a new grant."""
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select
from backend.app.application import commit_guard
from backend.app.modules.assistant import service, dispatch, progress
from backend.app.modules.assistant.core_adapter import digest, CoreError
from backend.app.modules.assistant.turn_adapter import DshTurnsAdapter
from backend.app.modules.assistant.models import AssistantWork, AssistantDispatch, AssistantTurn, AssistantTurnDispatch
from backend.app.modules.tasks import service as tasks
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.command_guard import lock_command_authority, check_command_authority

PERMISSIONS=('agents.run','tasks.manage','tasks.read_self')


def _root(db, actor, root_id, *, require_active=False):
    work=db.scalar(select(AssistantWork).where(*service._scope(actor),AssistantWork.task_id==root_id)
        .execution_options(populate_existing=True))
    if work is None:raise HTTPException(404,'assistant_work_not_found')
    view=service._view(db,actor,work,include_execution=False)
    if require_active and view['task_state'] not in ('accepted','assigned','in_progress'):
        raise HTTPException(409,'assistant_work_not_active')
    intent=db.get(AssistantDispatch,root_id,populate_existing=True)
    if intent is None:raise HTTPException(409,'assistant_followup_parent_unsettled')
    _,fact=progress._capture(db,work,intent)
    if fact['state']=='needs_attention':raise HTTPException(409,'assistant_followup_parent_unresolved')
    return work,view,fact


def _scope(actor):
    return (AssistantTurn.organization_id==actor.organization_id,AssistantTurn.store_id==actor.store_id,
            AssistantTurn.owner_id==actor.subject_id)


def admission(work, view):
    return dict(contract_version=1,operation='open',scope=work.snapshot['scope'],
        request_id=work.request_id,text=view['text'],title=view['title'],at=work.created_at.isoformat(),snapshot=None)


def source_view(db,actor,row,*,require_active=False):
    """Validate frozen sources without recursively inspecting any Run/grant."""
    work=db.scalar(select(AssistantWork).where(*service._scope(actor),AssistantWork.task_id==row.root_work_id))
    if work is None:raise HTTPException(404,'assistant_work_not_found')
    view=service._view(db,actor,work,include_execution=False)
    if require_active and view['task_state'] not in ('accepted','assigned','in_progress'):
        raise HTTPException(409,'assistant_work_not_active')
    chain=list(db.scalars(select(AssistantTurn).where(*_scope(actor),AssistantTurn.root_work_id==work.task_id,
        AssistantTurn.sequence<=row.sequence).order_by(AssistantTurn.sequence)))
    # Intake validation may occur just before this new row is flushed.
    if not chain or chain[-1].id!=row.id:chain.append(row)
    prefix=[];parent_id=work.task_id;parent_run=db.get(AssistantDispatch,work.task_id).run_id
    try:
        for sequence,part in enumerate(chain,2):
            event=dict(kind='followup',turn_id=part.id,run_id=part.run_id,request_id=part.request_id,
                text=part.text,at=part.created_at.isoformat())
            fact=part.parent_fact
            expected=prefix+[dict(kind='result',run_id=fact['run_id'],state=fact['state'],at=fact['at']),event]
            ticket=part.snapshot['tickets'][0]
            if ((part.organization_id,part.store_id,part.owner_id)!=(actor.organization_id,actor.store_id,actor.subject_id)
                    or part.previous_turn_id!=parent_id or part.sequence!=sequence or part.state!='received'
                    or fact['work_id']!=parent_id or fact['run_id']!=parent_run
                    or fact['state'] not in ('completed','failed','cancelled','blocked')
                    or part.parent_fact_sha256!=digest(fact) or part.history!=expected
                    or part.history_sha256!=digest(part.history) or part.request_digest!=digest({'text':part.text})
                    or part.snapshot_sha256!=digest(part.snapshot) or part.snapshot['scope']!=work.snapshot['scope']
                    or part.ticket_id!=work.ticket_id or ticket['id']!=work.ticket_id
                    or len(part.snapshot['tickets'])!=1 or len(ticket['assignments'])!=sequence
                    or ticket['assignments'][-1]['id']!=part.assignment_id
                    or [t['text'] for t in part.snapshot['triggers']]!=[view['text']]+[e['text'] for e in expected if e['kind']=='followup']):
                raise ValueError()
            from backend.app.modules.assistant.inbox_execution import verify_origin
            verify_origin(db, part)
            prefix=part.history;parent_id=part.id;parent_run=part.run_id
        if chain[-1].id!=row.id or len(chain)!=row.sequence-1:raise ValueError()
    except (ValueError,KeyError,IndexError,TypeError,AttributeError):
        raise HTTPException(409,'assistant_turn_binding_conflict') from None
    return work,view


def execution_unresolved(db,run_id):
    from backend.app.modules.agents.models import AgentExecution
    execution=db.get(AgentExecution,run_id,populate_existing=True)
    return execution is not None and (execution.effect_status=='external_unknown'
        or execution.cancel_resolution=='unknown' or execution.phase=='unknown')


def _parent_fact(db,actor,work_id,previous):
    if previous==work_id:fact=_root(db,actor,work_id)[2]
    else:
        from backend.app.modules.assistant.turn_execution import capture
        row=db.scalar(select(AssistantTurn).where(*_scope(actor),AssistantTurn.id==previous,
            AssistantTurn.root_work_id==work_id))
        intent=db.get(AssistantTurnDispatch,previous)
        if row is None or intent is None:raise HTTPException(503,'assistant_followup_execution_not_connected')
        fact=capture(db,row,intent)
    if fact['state']=='needs_attention' or execution_unresolved(db,fact['run_id']):
        raise HTTPException(409,'assistant_followup_parent_unresolved')
    return fact


def _validate(db,actor,row,*,require_active=False):
    source_view(db,actor,row,require_active=require_active)
    if row.parent_fact!=_parent_fact(db,actor,row.root_work_id,row.previous_turn_id):
        raise HTTPException(409,'assistant_turn_binding_conflict')
    return row


def _seal(row):
    values={column.name:getattr(row,column.name) for column in AssistantTurn.__table__.columns}
    values['created_at']=row.created_at.isoformat()
    return digest(values)


def _view(row,replayed=False):
    return dict(turn_id=row.id,root_work_id=row.root_work_id,previous_turn_id=row.previous_turn_id,
        sequence=row.sequence,request_id=row.request_id,text=row.text,created_at=row.created_at.isoformat(),
        intake_state='received',execution_status='not_dispatched',replayed=replayed)


def _build_turn(db, actor, work, view, latest, fact, *, text, request_id, core, origin_account_id):
    """Pure source fold; callers must independently authorize atomic intake."""
    sequence=latest.sequence+1 if latest is not None else 2
    now=commit_guard._db_clock(db);turn_id,run_id=str(uuid4()),str(uuid4())
    history=(latest.history if latest is not None else [])+[dict(kind='result',run_id=fact['run_id'],state=fact['state'],at=fact['at']),
        dict(kind='followup',turn_id=turn_id,run_id=run_id,request_id=request_id,text=text,at=now.isoformat())]
    try:result=core.fold(admission=admission(work,view),root_run_id=history[0]['run_id'],history=history)
    except CoreError:raise HTTPException(503,'assistant_followup_unavailable') from None
    if result['ticket_id']!=work.ticket_id or result['state']!='pending' or result['sequence']!=sequence:
        raise HTTPException(409,'assistant_turn_binding_conflict')
    row=AssistantTurn(id=turn_id,root_work_id=work.task_id,previous_turn_id=latest.id if latest else work.task_id,sequence=sequence,
        organization_id=actor.organization_id,store_id=actor.store_id,owner_id=actor.subject_id,
        origin_account_id=origin_account_id,request_id=request_id,request_digest=digest({'text':text}),text=text,
        run_id=run_id,ticket_id=result['ticket_id'],assignment_id=result['assignment_id'],parent_fact=fact,
        parent_fact_sha256=digest(fact),history=history,history_sha256=digest(history),snapshot=result['snapshot'],
        snapshot_sha256=digest(result['snapshot']),manifest_sha256=core.config.manifest_sha256,state='received',created_at=now)
    return row


def append_followup(db,actor,*,root_work_id,previous_turn_id,text,request_id,core,authority_policy=None):
    """Caller owns commit; received-only legacy rows are never backfilled."""
    service._check_request(text,request_id)
    service._check_request('root',root_work_id);service._check_request('previous',previous_turn_id)
    if type(core) is not DshTurnsAdapter:raise HTTPException(503,'assistant_followup_unavailable')
    epoch=lock_command_authority(db,actor)
    for permission in PERMISSIONS:service._authorize(db,actor,permission)
    tasks._require_person_actor(db,actor)
    work,view,fact=_root(db,actor,root_work_id,require_active=True)
    # The command fence serializes concurrent appenders before the unique key.
    existing=db.scalar(select(AssistantTurn).where(*_scope(actor),AssistantTurn.request_id==request_id))
    if existing is not None:
        if (existing.root_work_id!=root_work_id or existing.previous_turn_id!=previous_turn_id
                or existing.request_digest!=digest({'text':text}) or existing.manifest_sha256!=core.config.manifest_sha256):
            raise HTTPException(409,'assistant_followup_request_conflict')
        row=_validate(db,actor,existing);replayed=True;captured=_seal(row)
    else:
        from backend.app.modules.assistant.models import AssistantInbox
        if db.scalar(select(AssistantInbox.id).where(AssistantInbox.organization_id == actor.organization_id,
                AssistantInbox.store_id == actor.store_id, AssistantInbox.owner_id == actor.subject_id,
                AssistantInbox.request_id == request_id)):
            raise HTTPException(409,'assistant_followup_request_conflict')
        if db.scalar(select(AssistantWork).where(*service._scope(actor),AssistantWork.request_id==request_id)):
            raise HTTPException(409,'assistant_followup_request_conflict')
        latest=db.scalar(select(AssistantTurn).where(*_scope(actor),AssistantTurn.root_work_id==root_work_id)
            .order_by(AssistantTurn.sequence.desc()).limit(1))
        expected=latest.id if latest is not None else root_work_id
        if previous_turn_id!=expected:raise HTTPException(409,'assistant_followup_parent_changed')
        if latest is not None and authority_policy is None:
            raise HTTPException(503,'assistant_followup_execution_not_connected')
        fact=_parent_fact(db,actor,root_work_id,previous_turn_id)
        row=_build_turn(db,actor,work,view,latest,fact,text=text,request_id=request_id,
            core=core,origin_account_id=actor.account_id)
        turn_id=row.id
        captured=_seal(row)
        if authority_policy is not None:
            from backend.app.modules.assistant.turn_execution import intake
            intake(db,actor,row,authority_policy)
        else:db.add(row)
        record_event(db,actor_id=actor.subject_id,organization_id=actor.organization_id,store_id=actor.store_id,
            action='assistant.followup_received',target_type='task',target_id=root_work_id,
            idempotency_key='assistant-followup:'+actor.subject_id+':'+request_id,
            details=dict(turn_id=turn_id,previous_turn_id=previous_turn_id,request_digest=row.request_digest,
                parent_fact_sha256=row.parent_fact_sha256,snapshot_sha256=row.snapshot_sha256),occurred_at=row.created_at)
        db.flush();replayed=False
    def final(session,now):
        for permission in PERMISSIONS:check_command_authority(session,actor,permission=permission,epoch=epoch,now=now)
        session.refresh(row);_validate(session,actor,row,require_active=True)
        if _seal(row)!=captured:raise HTTPException(409,'assistant_turn_commit_conflict')
    commit_guard.enlist(db,action='assistant.followup',check=final)
    return _full_view(db,actor,row,replayed)


def read_turn(db,actor,turn_id):
    service._check_request('read',turn_id)
    for permission in ('agents.run','tasks.read_self'):service._authorize(db,actor,permission)
    row=db.scalar(select(AssistantTurn).where(*_scope(actor),AssistantTurn.id==turn_id))
    if row is None:raise HTTPException(404,'assistant_turn_not_found')
    return _full_view(db,actor,_validate(db,actor,row))


def _full_view(db,actor,row,replayed=False):
    from backend.app.modules.assistant.turn_execution import execution_view
    value=_view(row,replayed)
    if db.get(AssistantTurnDispatch,row.id) is not None:value.update(execution_view(db,actor,row))
    return value


def list_turns(db,actor,root_work_id):
    service.read_work(db,actor,root_work_id)
    rows=db.scalars(select(AssistantTurn).where(*_scope(actor),AssistantTurn.root_work_id==root_work_id)
        .order_by(AssistantTurn.sequence)).all()
    return [_full_view(db,actor,_validate(db,actor,row)) for row in rows]


def read_request(db,actor,root_work_id,request_id):
    service._check_request('request',request_id)
    service.read_work(db,actor,root_work_id)
    row=db.scalar(select(AssistantTurn).where(*_scope(actor),AssistantTurn.root_work_id==root_work_id,
        AssistantTurn.request_id==request_id))
    if row is None:raise HTTPException(404,'assistant_turn_not_found')
    return _full_view(db,actor,_validate(db,actor,row))
