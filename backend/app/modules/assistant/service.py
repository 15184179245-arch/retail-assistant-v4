"""Explicit work intake, atomically bound to Task, DSH snapshot and Host outbox."""
from contextlib import contextmanager
from uuid import UUID, uuid4
from backend.app.modules.assistant.input_capacity import check_input_capacity
from fastapi import HTTPException
from sqlalchemy import select, or_, and_
from backend.app.application import commit_guard
from backend.app.modules.auth.service import validate_actor_session
from backend.app.modules.identity.command_guard import lock_command_authority, check_command_authority, refresh_authority
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.tasks import service as tasks
from backend.app.modules.tasks.models import Task
from backend.app.modules.audit.service import record_event
from backend.app.workers.outbox import OutboxEvent, enqueue
from backend.app.modules.assistant.core_adapter import CORE_COMMIT, CoreError, DshCoreAdapter, digest
from backend.app.modules.assistant.models import AssistantWork, AssistantDispatch

EVENT_TYPE = 'assistant.pi.requested.v1'


def _scope(actor):
    return (AssistantWork.organization_id == actor.organization_id,
            AssistantWork.store_id == actor.store_id, AssistantWork.owner_id == actor.subject_id)


def _authorize(session, actor, permission):
    validate_actor_session(session, actor)
    authorize_scope(session, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission=permission)


@contextmanager
def _history_read_authority(session, actor):
    """Refresh current authority; never flush caller-owned pending work."""
    with session.no_autoflush:
        refresh_authority(session)
        _authorize(session, actor, 'agents.run')
        _authorize(session, actor, 'tasks.read_self')
        yield


def _check_request(text, request_id):
    try:
        if (type(request_id) is not str or str(UUID(request_id)) != request_id
                or type(text) is not str or not text.strip()
                or '\x00' in text or any(0xD800 <= ord(c) <= 0xDFFF for c in text)):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, 'assistant_request_invalid') from None
    check_input_capacity(text)


def _effect_payload(work):
    return dict(work_id=work.task_id, owner_id=work.owner_id, core_commit=work.core_commit,
        ticket_id=work.ticket_id, assignment_id=work.assignment_id, snapshot_sha256=work.snapshot_sha256)


def _view(session, actor, work, *, include_execution=True):
    task = session.scalar(select(Task).where(Task.id == work.task_id,
        Task.organization_id == actor.organization_id, Task.store_id == actor.store_id,
        Task.created_by == actor.subject_id))
    event = session.scalar(select(OutboxEvent).where(OutboxEvent.id == work.outbox_event_id,
        OutboxEvent.organization_id == actor.organization_id, OutboxEvent.store_id == actor.store_id))
    expected_scope = dict(organization_id=work.organization_id, store_id=work.store_id,
                          owner_id=work.owner_id, task_id=work.task_id)
    if (task is None or event is None or work.core_commit != CORE_COMMIT
            or type(work.snapshot) is not dict
            or digest(work.snapshot) != work.snapshot_sha256
            or work.snapshot.get('scope') != expected_scope
            or work.request_digest != digest({'text': task.description})
            or event.event_type != EVENT_TYPE or event.payload != _effect_payload(work)):
        raise HTTPException(409, 'assistant_work_binding_conflict')
    result = dict(work_id=work.task_id, request_id=work.request_id, title=task.title,
        text=task.description, task_state=task.state,
        execution_status='queued' if event.status in ('pending', 'retry') else 'dispatch_unverified',
        version=work.version, created_at=work.created_at.isoformat())
    if include_execution:
        from backend.app.modules.assistant.dispatch import execution_view
        result.update(execution_view(session, actor, work))
    return result


def submit_work(session, actor, *, text, request_id, core, authority_policy=None):
    """Caller owns commit. An explicit request is accepted, not proposed again."""
    _check_request(text, request_id)
    if type(core) is not DshCoreAdapter:
        raise HTTPException(503, 'assistant_intake_unavailable')
    epoch = lock_command_authority(session, actor)
    _authorize(session, actor, 'agents.run')
    _authorize(session, actor, 'tasks.manage')
    def final(db, now):
        check_command_authority(db, actor, permission='agents.run', epoch=epoch, now=now)
        check_command_authority(db, actor, permission='tasks.manage', epoch=epoch, now=now)
    commit_guard.enlist(session, action='assistant.request', check=final)
    existing = session.scalar(select(AssistantWork).where(*_scope(actor),
        AssistantWork.request_id == request_id).with_for_update())
    if existing is not None:
        if existing.request_digest != digest({'text': text}):
            raise HTTPException(409, 'assistant_request_conflict')
        return {**_view(session, actor, existing), 'replayed': True}
    from backend.app.modules.assistant.models import AssistantInbox, AssistantTurn
    for model in (AssistantInbox, AssistantTurn):
        if session.scalar(select(model.id).where(model.organization_id == actor.organization_id,
                model.store_id == actor.store_id, model.owner_id == actor.subject_id,
                model.request_id == request_id)):
            raise HTTPException(409, 'assistant_request_conflict')
    key = f'assistant:{actor.subject_id}:{request_id}'
    task = tasks.create_managed_task(session, actor, title=text.strip()[:200], description=text,
        reason='用户明确交代助理工作', idempotency_key=key)
    at = commit_guard._db_clock(session)
    scope = dict(organization_id=actor.organization_id, store_id=actor.store_id,
                 owner_id=actor.subject_id, task_id=task.id)
    try:
        result = core.apply(scope=scope, request_id=request_id, text=text, title=task.title, at=at.isoformat())
    except CoreError:
        raise HTTPException(503, 'assistant_intake_unavailable') from None
    work = AssistantWork(task_id=task.id, organization_id=actor.organization_id, store_id=actor.store_id,
        owner_id=actor.subject_id, origin_account_id=actor.account_id, request_id=request_id,
        request_digest=digest({'text': text}), core_commit=CORE_COMMIT,
        ticket_id=result['ticket_id'], assignment_id=result['assignment_id'], snapshot=result['snapshot'],
        snapshot_sha256=digest(result['snapshot']), version=1, created_at=at)
    effect = result['effects'][0]
    event = enqueue(session, organization_id=actor.organization_id, store_id=actor.store_id,
        event_type=EVENT_TYPE, idempotency_key='assistant-start:' + effect['id'],
        payload=_effect_payload(work), now=at)
    work.outbox_event_id = event.id
    session.add(work)
    dispatch = AssistantDispatch(work_id=work.task_id, run_id=str(uuid4()),
        session_id=actor.session_id, account_version=actor.account_version,
        authorization_expires_at=actor.auth_expires_at, state='pending')
    session.add(dispatch)
    if authority_policy is not None:
        from backend.app.modules.assistant.authority import issue_work_authority
        issue_work_authority(session, actor, work, dispatch, authority_policy)
    else:
        session.flush([work])
    record_event(session, actor_id=actor.subject_id, organization_id=actor.organization_id,
        store_id=actor.store_id, action='assistant.work_received', target_type='task', target_id=task.id,
        idempotency_key=key, details=dict(request_id=request_id, request_digest=work.request_digest,
            core_commit=CORE_COMMIT, snapshot_sha256=work.snapshot_sha256,
            outbox_event_id=event.id, ticket_id=work.ticket_id, assignment_id=work.assignment_id), occurred_at=at)
    session.flush()
    return {**_view(session, actor, work), 'replayed': False}


def read_work(session, actor, work_id):
    with _history_read_authority(session, actor):
        work = session.scalar(select(AssistantWork).where(*_scope(actor), AssistantWork.task_id == work_id))
        if work is None:
            raise HTTPException(404, 'assistant_work_not_found')
        return _view(session, actor, work)


def read_request(session, actor, request_id):
    _check_request('read', request_id)
    with _history_read_authority(session, actor):
        work = session.scalar(select(AssistantWork).where(*_scope(actor), AssistantWork.request_id == request_id))
        return dict(request_id=request_id, status='received' if work is not None else 'unobserved',
                    work=_view(session, actor, work) if work is not None else None)


def list_work(session, actor, *, before_work_id=None):
    with _history_read_authority(session, actor):
        query = select(AssistantWork).where(*_scope(actor))
        if before_work_id is not None:
            _check_request('read', before_work_id)
            anchor = session.scalar(select(AssistantWork).where(*_scope(actor), AssistantWork.task_id == before_work_id))
            if anchor is None:
                raise HTTPException(404, 'assistant_work_not_found')
            query = query.where(or_(AssistantWork.created_at < anchor.created_at,
                and_(AssistantWork.created_at == anchor.created_at, AssistantWork.task_id < anchor.task_id)))
        rows = session.scalars(query.order_by(
            AssistantWork.created_at.desc(), AssistantWork.task_id.desc()).limit(50))
        return [_view(session, actor, row) for row in rows]
