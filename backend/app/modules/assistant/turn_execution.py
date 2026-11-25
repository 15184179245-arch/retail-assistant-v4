"""Exact-turn Host integration; the original business Task is never recreated."""
from dataclasses import dataclass
from hashlib import sha256
from fastapi import HTTPException
from backend.app.application import commit_guard
from backend.app.modules.assistant.models import AssistantTurnDispatch
from backend.app.modules.assistant.core_adapter import digest
from backend.app.modules.assistant.input_capacity import compose_turn_input
from backend.app.modules.assistant.authority import issue_work_authority
from backend.app.workers.outbox import OutboxEvent, enqueue

EVENT_TYPE='assistant.pi.turn.requested.v1'


@dataclass(frozen=True)
class Scope:
    organization_id: str
    store_id: str
    subject_id: str


def scope(turn):
    return Scope(turn.organization_id,turn.store_id,turn.owner_id)


def payload(turn):
    return dict(turn_id=turn.id,root_work_id=turn.root_work_id,run_id=turn.run_id,
        request_digest=turn.request_digest,snapshot_sha256=turn.snapshot_sha256)


def intake(db,actor,turn,policy):
    if policy is None:raise HTTPException(503,'assistant_turn_authority_required')
    event=enqueue(db,organization_id=turn.organization_id,store_id=turn.store_id,event_type=EVENT_TYPE,
        idempotency_key='assistant-turn:'+turn.id,payload=payload(turn),now=turn.created_at)
    intent=AssistantTurnDispatch(work_id=turn.id,run_id=turn.run_id,session_id=actor.session_id,
        account_version=actor.account_version,authorization_expires_at=actor.auth_expires_at,
        state='pending',outbox_event_id=event.id)
    db.add(turn);db.add(intent)
    issue_work_authority(db,actor,turn,intent,policy,kind='turn')
    return intent


def binding(db,turn,intent):
    from backend.app.modules.assistant import turns
    root,view=turns.source_view(db,scope(turn),turn)
    event=db.get(OutboxEvent,intent.outbox_event_id)
    if (intent.work_id!=turn.id or intent.run_id!=turn.run_id or event is None
            or (event.organization_id,event.store_id)!=(turn.organization_id,turn.store_id)
            or event.event_type!=EVENT_TYPE or event.payload!=payload(turn)):
        raise HTTPException(409,'assistant_turn_dispatch_binding_conflict')
    # All included text remains user-supplied data, not a system policy or a
    # reconstructed human credential. Never silently truncate the new request.
    text=compose_turn_input(turn.root_work_id, view['text'], turn.history)
    return dict(task_state=view['task_state'],title=view['title'],text=text)


def capture(db,turn,intent):
    from backend.app.modules.assistant import dispatch as worker
    from backend.app.modules.assistant.control import PRELAUNCH_STOP
    binding(db,turn,intent)
    if intent.state not in ('settled','denied','unknown') or intent.finished_at is None:
        raise HTTPException(409,'assistant_followup_parent_unsettled')
    run=worker._bound_run(db,turn,intent)
    event=db.get(OutboxEvent,intent.outbox_event_id)
    state='needs_attention'
    if intent.state=='settled':
        if (run is None or run.state not in ('completed','failed','cancelled')
                or event.status!='delivered' or event.external_receipt!='agent-run:'+run.id):
            raise HTTPException(409,'assistant_turn_fact_conflict')
        from backend.app.modules.assistant.turns import execution_unresolved
        state='needs_attention' if execution_unresolved(db,run.id) else run.state
    elif intent.state=='denied':
        if run is not None or event.status!='dead':raise HTTPException(409,'assistant_turn_fact_conflict')
        state='cancelled' if intent.code==PRELAUNCH_STOP else 'blocked'
    elif event.status!='dead':raise HTTPException(409,'assistant_turn_fact_conflict')
    return dict(work_id=turn.id,run_id=intent.run_id,state=state,at=intent.finished_at.isoformat(),
        admission_sha256=turn.snapshot_sha256,request_digest=turn.request_digest,
        dispatch_state=intent.state,dispatch_code=intent.code,runtime_sha256=digest(intent.runtime_record),
        run_state=run.state if run else None,run_version=run.version if run else None,
        outbox_status=event.status,outbox_receipt=event.external_receipt)


def settle(db,config,turn,intent):
    from backend.app.modules.assistant import turns
    if config.turns is None:raise HTTPException(503,'assistant_followup_unavailable')
    if turn.manifest_sha256!=config.turns.config.manifest_sha256:raise HTTPException(409,'assistant_turn_source_conflict')
    fact=capture(db,turn,intent)
    root,view=turns.source_view(db,scope(turn),turn)
    history=turn.history+[dict(kind='result',run_id=fact['run_id'],state=fact['state'],at=fact['at'])]
    projection=config.turns.fold(admission=turns.admission(root,view),root_run_id=turn.history[0]['run_id'],history=history)
    record=dict(fact=fact,history=history,projection=projection)
    if intent.result_record is not None and (intent.result_record!=record or intent.result_sha256!=digest(record)):
        raise HTTPException(409,'assistant_turn_result_conflict')
    intent.result_record=record;intent.result_sha256=digest(record)
    expected=intent.result_sha256
    def final(session,now):
        session.refresh(turn);session.refresh(intent)
        if (capture(session,turn,intent)!=fact or intent.result_sha256!=expected
                or digest(intent.result_record)!=expected):raise HTTPException(409,'assistant_turn_result_conflict')
    commit_guard.enlist(db,action='assistant.turn_result',check=final)


def execution_view(db,actor,turn):
    from backend.app.modules.assistant import dispatch as worker
    intent=db.get(AssistantTurnDispatch,turn.id)
    if intent is None:return dict(execution_status='not_dispatched',run_id=None,run_receipt=None)
    return worker.execution_view(db,actor,turn)


def stop_turn(session,actor,root_work_id,turn_id):
    """Current human owner stops this exact turn; never renews another grant."""
    from backend.app.modules.assistant import turns, service, dispatch as worker
    from backend.app.modules.assistant.models import AssistantTurn
    from backend.app.modules.assistant.control import PRELAUNCH_STOP, _result
    from backend.app.modules.assistant.authority import revoke_work_authority
    from backend.app.modules.agents import service as agents
    from backend.app.modules.identity.command_guard import lock_command_authority,check_command_authority
    from backend.app.modules.audit.service import record_event
    epoch=lock_command_authority(session,actor)
    for permission in turns.PERMISSIONS:service._authorize(session,actor,permission)
    agents._scope(session,actor)
    view=turns.read_turn(session,actor,turn_id)
    if view['root_work_id']!=root_work_id:raise HTTPException(404,'assistant_turn_not_found')
    turn=session.get(AssistantTurn,turn_id)
    intent=session.get(AssistantTurnDispatch,turn_id,populate_existing=True)
    if intent is None:raise HTTPException(409,'assistant_stop_authority_unrecorded')
    def final(db,now):
        for permission in turns.PERMISSIONS:check_command_authority(db,actor,permission=permission,epoch=epoch,now=now)
    commit_guard.enlist(session,action='assistant.turn_stop',check=final)
    from backend.app.modules.assistant import inbox
    inbox.cancel_pending(session,actor,root_work_id)
    revoke_work_authority(session,actor,turn_id,kind='turn')
    if intent.state=='pending':
        event=session.get(OutboxEvent,intent.outbox_event_id)
        if view['run_id'] is not None or event.status!='pending' or event.attempts!=0 or intent.runtime_record is not None:
            raise HTTPException(409,'assistant_stop_dispatch_conflict')
        now=commit_guard._db_clock(session)
        intent.state,intent.code,intent.finished_at='denied',PRELAUNCH_STOP,now
        event.status,event.last_error='dead',PRELAUNCH_STOP
        record_event(session,actor_id=actor.subject_id,organization_id=actor.organization_id,store_id=actor.store_id,
            action='assistant.turn_stopped_before_launch',target_type='assistant_turn',target_id=turn_id,
            idempotency_key='assistant-turn-stop:'+turn_id,details={'root_work_id':root_work_id,'code':PRELAUNCH_STOP},occurred_at=now)
        session.commit()
    elif view['execution_status'] in ('cancelled','completed','failed','blocked','needs_attention','dispatch_unverified'):
        session.commit()
    elif view['run_id'] is None:
        raise HTTPException(409,'assistant_stop_starting')
    else:agents.cancel_run(session,actor,view['run_id'])
    return _result(turns.read_turn(session,actor,turn_id))
