"""Owner-scoped, monotonic stop command. Never starts or repeats execution."""
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.application import commit_guard
from backend.app.modules.assistant import service
from backend.app.modules.assistant.models import AssistantWork, AssistantDispatch
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.command_guard import lock_command_authority, check_command_authority
from backend.app.workers.outbox import OutboxEvent

PRELAUNCH_STOP = 'USER_STOPPED_BEFORE_LAUNCH'
_PERMISSIONS = ('agents.run', 'tasks.read_self', 'tasks.manage')


def _result(view):
    execution = (view.get('run_receipt') or {}).get('execution') or {}
    if (view['execution_status'] in ('needs_attention', 'dispatch_unverified')
            or execution.get('effect_status') == 'external_unknown'
            or execution.get('cancel_resolution') == 'unknown'):
        outcome = 'outcome_unresolved'
    elif view['execution_status'] == 'cancelled':
        outcome = 'stopped'
    elif view['execution_status'] in ('completed', 'failed', 'blocked'):
        outcome = 'already_finished'
    else:
        outcome = 'stop_requested'
    return {'work': view, 'stop_result': outcome}


def stop_work(session, actor, work_id):
    """Owns commit, like the existing Agent cancel command. No new authority."""
    epoch = lock_command_authority(session, actor)
    for permission in _PERMISSIONS:
        service._authorize(session, actor, permission)
    from backend.app.modules.agents import service as agents
    agents._scope(session, actor)  # Also require a person, including prelaunch stops.
    def final(db, now):
        for permission in _PERMISSIONS:
            check_command_authority(db, actor, permission=permission, epoch=epoch, now=now)
    commit_guard.enlist(session, action='assistant.stop', check=final)
    work = session.scalar(select(AssistantWork).where(*service._scope(actor), AssistantWork.task_id == work_id))
    if work is None:
        raise HTTPException(404, 'assistant_work_not_found')
    # The Store/fence command lock serializes against dispatcher preparation
    # and the durable launch-intent transaction; do not reverse execution locks.
    dispatch = session.get(AssistantDispatch, work_id, populate_existing=True)
    view = service._view(session, actor, work)
    if dispatch is None:
        raise HTTPException(409, 'assistant_stop_authority_unrecorded')
    from backend.app.modules.assistant.authority import revoke_work_authority
    from backend.app.modules.assistant import inbox
    inbox.cancel_pending(session, actor, work_id)
    revoke_work_authority(session, actor, work_id)
    # A root stop covers the current same-Task turn as well as pending inbox.
    # Never rewrite the original root Run's terminal result.
    from backend.app.modules.assistant.models import AssistantTurn, AssistantTurnDispatch
    from backend.app.modules.assistant import turns
    latest = session.scalar(select(AssistantTurn).where(*turns._scope(actor),
        AssistantTurn.root_work_id == work_id).order_by(AssistantTurn.sequence.desc()).limit(1))
    if latest is not None and session.get(AssistantTurnDispatch, latest.id) is not None:
        from backend.app.modules.assistant.turn_execution import stop_turn
        stopped = stop_turn(session, actor, work_id, latest.id)
        result = _result(service.read_work(session, actor, work_id))
        result.update(stop_result=stopped['stop_result'], current_turn_id=latest.id)
        return result
    if dispatch.state == 'pending':
        event = session.get(OutboxEvent, work.outbox_event_id)
        if (view['run_id'] is not None or event is None or event.status != 'pending'
                or event.attempts != 0 or dispatch.runtime_record is not None):
            raise HTTPException(409, 'assistant_stop_dispatch_conflict')
        now = commit_guard._db_clock(session)
        dispatch.state, dispatch.code, dispatch.finished_at = 'denied', PRELAUNCH_STOP, now
        event.status, event.last_error = 'dead', PRELAUNCH_STOP
        record_event(session, actor_id=actor.subject_id, organization_id=actor.organization_id,
            store_id=actor.store_id, action='assistant.work_stopped_before_launch',
            target_type='task', target_id=work_id, idempotency_key='assistant-stop:' + work_id,
            details={'request_id': work.request_id, 'code': PRELAUNCH_STOP}, occurred_at=now)
        session.commit()
        return _result(service.read_work(session, actor, work_id))
    if view['execution_status'] in ('cancelled', 'completed', 'failed', 'blocked', 'needs_attention', 'dispatch_unverified'):
        # Terminal/unknown facts are not overwritten by a late stop click.
        session.commit()
        return _result(service.read_work(session, actor, work_id))
    if view['run_id'] is None:
        # A durable launch may already be in progress. No fabricated receipt
        # and no attempt to turn its outbox back into an unstarted request.
        raise HTTPException(409, 'assistant_stop_starting')
    agents.cancel_run(session, actor, view['run_id'])
    return _result(service.read_work(session, actor, work_id))
