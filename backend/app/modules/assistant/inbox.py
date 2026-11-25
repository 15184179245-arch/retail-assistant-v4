"""Durable in-flight instructions. No implicit retry or fake parent result.

The Host supplies finite consent at receipt time. This module does not turn it
into an execution principal. inbox_execution owns transactional conversion
from finite consent to one turn after validating the real settled parent.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.application import commit_guard
from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.assistant import service, turns
from backend.app.modules.assistant.authority import WorkAuthorityPolicy, _utc
from backend.app.modules.assistant.core_adapter import digest
from backend.app.modules.assistant.models import AssistantInbox, AssistantWork, AssistantDispatch, AssistantTurn
from backend.app.modules.assistant.principal import CAPABILITIES
from backend.app.modules.audit.service import record_event
from backend.app.modules.auth.models import EmployeeAccount, LoginSession
from backend.app.modules.identity.command_guard import (
    lock_command_authority, check_command_authority, lock_work_security_fence, refresh_authority)
from backend.app.modules.identity.models import ScopeSecurityFence
from backend.app.modules.identity.repository import load_scope, grants_for
from backend.app.modules.identity.service import permission_allowed

REQUIRED = frozenset(('agents.run', 'tasks.manage', 'tasks.read_self'))


def fail(code='assistant_inbox_binding_conflict', status=409):
    raise HTTPException(status, code)


def _scope(actor):
    return (AssistantInbox.organization_id == actor.organization_id,
            AssistantInbox.store_id == actor.store_id, AssistantInbox.owner_id == actor.subject_id)


@dataclass(frozen=True)
class Scope:
    organization_id: str
    store_id: str
    subject_id: str


def _source(db, actor, root_id, parent_id, *, latest=False, active=False, verify_parent=None):
    root = db.scalar(select(AssistantWork).where(*service._scope(actor), AssistantWork.task_id == root_id)
                     .execution_options(populate_existing=True))
    if root is None: fail('assistant_work_not_found', 404)
    view = service._view(db, actor, root, include_execution=False)
    if active and view['task_state'] not in ('accepted', 'assigned', 'in_progress'):
        fail('assistant_work_not_active')
    if latest:
        last = db.scalar(select(AssistantTurn).where(*turns._scope(actor), AssistantTurn.root_work_id == root_id)
                         .order_by(AssistantTurn.sequence.desc()).limit(1))
        if parent_id != (last.id if last else root_id): fail('assistant_inbox_parent_changed')
    if parent_id == root_id:
        intent = db.get(AssistantDispatch, root_id, populate_existing=True)
        if intent is None: fail('assistant_inbox_parent_unavailable')
        run_id, parent_sha, parent_request = intent.run_id, root.snapshot_sha256, root.request_digest
    else:
        parent = db.scalar(select(AssistantTurn).where(*turns._scope(actor), AssistantTurn.id == parent_id,
                          AssistantTurn.root_work_id == root_id).execution_options(populate_existing=True))
        if parent is None: fail('assistant_inbox_parent_unavailable')
        if verify_parent is None:
            turns.source_view(db, actor, parent)
        else:
            verify_parent(root, view, parent)
        run_id, parent_sha, parent_request = parent.run_id, parent.snapshot_sha256, parent.request_digest
    return run_id, digest(dict(root=root.snapshot_sha256, root_request=root.request_digest, parent=parent_sha,
                               parent_request=parent_request, parent_id=parent_id, run_id=run_id))


def _seal(row):
    value = {c.name: getattr(row, c.name) for c in AssistantInbox.__table__.columns
             if c.name not in ('binding_sha256', 'cancelled_at')}
    for key in ('issued_at', 'expires_at'): value[key] = _utc(value[key]).isoformat()
    return digest(value)


def _verify(db, row, *, active=False, verify_parent=None):
    if (row.binding_sha256 != _seal(row) or row.request_digest != digest({'text': row.text})
            or type(row.permissions) is not list or any(type(p) is not str for p in row.permissions)
            or row.permissions != sorted(set(row.permissions))
            or not REQUIRED <= set(row.permissions) <= CAPABILITIES): fail()
    actor = Scope(row.organization_id, row.store_id, row.owner_id)
    if _source(db, actor, row.root_work_id, row.parent_id, active=active,
               verify_parent=verify_parent) != (row.parent_run_id, row.parent_source_sha256): fail()
    previous = db.get(AssistantInbox, row.previous_message_id, populate_existing=True) if row.previous_message_id else None
    if row.sequence == 1:
        if row.previous_message_id is not None: fail()
    elif (previous is None or previous.root_work_id != row.root_work_id
          or previous.sequence != row.sequence-1 or previous.owner_id != row.owner_id
          or previous.binding_sha256 != _seal(previous)): fail()
    return row


def _view(row, *, replayed=False):
    return dict(message_id=row.id, root_work_id=row.root_work_id, parent_id=row.parent_id,
                previous_message_id=row.previous_message_id, sequence=row.sequence,
                request_id=row.request_id, text=row.text, created_at=_utc(row.issued_at).isoformat(),
                intake_state='cancelled' if row.cancelled_at else 'received',
                execution_status='not_dispatched', replayed=replayed,
                authority_expires_at=_utc(row.expires_at).isoformat())


def _full_view(db, actor, row, *, replayed=False):
    from backend.app.modules.assistant.inbox_execution import converted
    value = _view(row, replayed=replayed)
    from backend.app.modules.assistant.workspace_binding import root_context, context_view
    root = db.get(AssistantWork, row.root_work_id, populate_existing=True)
    if root is None: fail()
    value['workspace_context'] = context_view(root_context(db, root))
    turn = converted(db, row)
    if turn is not None:
        execution = turns._full_view(db, actor, turns._validate(db, actor, turn))
        value.update(intake_state='converted', turn_id=turn.id,
                     execution_status=execution['execution_status'], run_id=execution.get('run_id'),
                     run_receipt=execution.get('run_receipt'), dispatch_code=execution.get('dispatch_code'),
                     dispatch_state=execution.get('dispatch_state'))
    return value


def _human(db, actor, *, write=False):
    if type(actor) is not AuthenticatedSubject or actor.account_id is None or actor.session_id is None:
        fail('assistant_inbox_human_required', 403)
    for key in (REQUIRED if write else ('agents.run', 'tasks.read_self')): service._authorize(db, actor, key)
    from backend.app.modules.tasks.service import _require_person_actor
    _require_person_actor(db, actor)


def receive(db, actor, *, root_work_id, parent_id, text, request_id, policy):
    """Caller owns commit; a receipt is not evidence of a new Pi execution."""
    service._check_request(text, request_id)
    service._check_request('root', root_work_id); service._check_request('parent', parent_id)
    if type(actor) is not AuthenticatedSubject: fail('assistant_inbox_human_required', 403)
    epoch = lock_command_authority(db, actor); _human(db, actor, write=True)
    existing = db.scalar(select(AssistantInbox).where(*_scope(actor), AssistantInbox.request_id == request_id))
    if existing is not None:
        if (existing.root_work_id, existing.parent_id, existing.request_digest) != (root_work_id, parent_id, digest({'text': text})):
            fail('assistant_inbox_request_conflict')
        return _full_view(db, actor, _verify(db, existing), replayed=True)
    # Do not adopt an old work/followup request id as a second instruction.
    if (db.scalar(select(AssistantWork.task_id).where(*service._scope(actor), AssistantWork.request_id == request_id))
            or db.scalar(select(AssistantTurn.id).where(*turns._scope(actor), AssistantTurn.request_id == request_id))):
        fail('assistant_inbox_request_conflict')
    from backend.app.modules.assistant.workspace_binding import root_context, augment_policy
    root = db.scalar(select(AssistantWork).where(*service._scope(actor), AssistantWork.task_id == root_work_id))
    if root is None: fail('assistant_work_not_found', 404)
    policy = augment_policy(db, actor, policy, root_context(db, root))
    now = commit_guard._db_clock(db)
    if (type(policy) is not WorkAuthorityPolicy or type(policy.permissions) is not tuple
            or any(type(p) is not str for p in policy.permissions)
            or len(set(policy.permissions)) != len(policy.permissions)
            or not REQUIRED <= set(policy.permissions) <= CAPABILITIES
            or type(policy.expires_at) is not datetime or policy.expires_at.tzinfo is None
            or policy.expires_at <= now): fail('assistant_inbox_policy_denied', 403)
    for key in policy.permissions: service._authorize(db, actor, key)
    run_id, source_sha = _source(db, actor, root_work_id, parent_id, latest=True, active=True)
    previous = db.scalar(select(AssistantInbox).where(*_scope(actor), AssistantInbox.root_work_id == root_work_id)
                         .order_by(AssistantInbox.sequence.desc()).limit(1))
    if previous is not None: _verify(db, previous)
    row = AssistantInbox(id=str(uuid4()), root_work_id=root_work_id, parent_id=parent_id,
        parent_run_id=run_id, parent_source_sha256=source_sha, previous_message_id=previous.id if previous else None,
        sequence=previous.sequence+1 if previous else 1, organization_id=actor.organization_id, store_id=actor.store_id,
        owner_id=actor.subject_id, account_id=actor.account_id, origin_session_id=actor.session_id,
        account_version=actor.account_version, work_epoch=lock_work_security_fence(db, actor.organization_id, actor.store_id),
        request_id=request_id, request_digest=digest({'text': text}), text=text, permissions=sorted(policy.permissions),
        issued_at=now, expires_at=policy.expires_at)
    row.binding_sha256 = _seal(row); expected = row.binding_sha256
    db.add(row); db.flush()
    def final(session, instant):
        for key in policy.permissions: check_command_authority(session, actor, permission=key, epoch=epoch, now=instant)
        session.refresh(row); _verify(session, row, active=True)
        if (row.binding_sha256 != expected or not _utc(row.issued_at) <= instant < _utc(row.expires_at)
                or _source(session, actor, root_work_id, parent_id, latest=True, active=True) != (run_id, source_sha)): fail()
    commit_guard.enlist(db, action='assistant.inbox_received', check=final)
    record_event(db, actor_id=actor.subject_id, organization_id=actor.organization_id, store_id=actor.store_id,
        action='assistant.inbox_received', target_type='assistant_inbox', target_id=row.id,
        idempotency_key='assistant-inbox:'+actor.subject_id+':'+request_id,
        details={'root_work_id':root_work_id, 'parent_id':parent_id, 'binding_sha256':expected}, occurred_at=now)
    return _full_view(db, actor, row)


def list_messages(db, actor, root_work_id):
    _human(db, actor); service.read_work(db, actor, root_work_id)
    rows = db.scalars(select(AssistantInbox).where(*_scope(actor), AssistantInbox.root_work_id == root_work_id)
                      .order_by(AssistantInbox.sequence)).all()
    return [_full_view(db, actor, _verify(db, row)) for row in rows]


def read_request(db, actor, root_work_id, request_id):
    _human(db, actor)
    row = db.scalar(select(AssistantInbox).where(*_scope(actor), AssistantInbox.root_work_id == root_work_id,
                     AssistantInbox.request_id == request_id))
    if row is None: fail('assistant_inbox_not_found', 404)
    return _full_view(db, actor, _verify(db, row))


def cancel(db, actor, message_id):
    epoch = lock_command_authority(db, actor); _human(db, actor, write=True)
    row = db.scalar(select(AssistantInbox).where(*_scope(actor), AssistantInbox.id == message_id))
    if row is None: fail('assistant_inbox_not_found', 404)
    _verify(db, row)
    from backend.app.modules.assistant.inbox_execution import converted
    if converted(db, row) is not None: fail('assistant_inbox_already_dispatched')
    if row.cancelled_at is None:
        row.cancelled_at = commit_guard._db_clock(db)
        record_event(db, actor_id=actor.subject_id, organization_id=actor.organization_id, store_id=actor.store_id,
            action='assistant.inbox_cancelled', target_type='assistant_inbox', target_id=row.id,
            idempotency_key='assistant-inbox-cancel:'+row.id, details={'binding_sha256':row.binding_sha256}, occurred_at=row.cancelled_at)
    expected = row.binding_sha256; cancelled = row.cancelled_at
    def final(session, instant):
        for key in REQUIRED: check_command_authority(session, actor, permission=key, epoch=epoch, now=instant)
        session.refresh(row); _verify(session, row)
        if row.binding_sha256 != expected or row.cancelled_at != cancelled: fail()
    commit_guard.enlist(db, action='assistant.inbox_cancelled', check=final)
    return _full_view(db, actor, row)


def check_authority(db, message_id, binding_sha256, *, permission='agents.run', now=None):
    """Fresh finite-consent check only. No principal, Run, conversion or commit.

    A future converter must hold the work security/store locks and enlist this
    same check at commit. Receipt readback does not require unexpired consent.
    """
    instant = now or datetime.now(timezone.utc)
    with db.no_autoflush:
        refresh_authority(db)
        row = db.get(AssistantInbox, message_id, populate_existing=True)
        if (row is None or row.binding_sha256 != binding_sha256 or row.cancelled_at is not None
                or permission not in CAPABILITIES or not _utc(row.issued_at) <= instant < _utc(row.expires_at)):
            fail('assistant_inbox_authority_denied', 403)
        _verify(db, row, active=True)
        # Consent predating a parent/root stop cannot become later work. A new
        # explicit instruction received after a stop has its own finite consent.
        from backend.app.modules.assistant.models import AssistantWorkAuthority, AssistantTurnAuthority, AssistantTurnDispatch
        from backend.app.modules.agents.models import AgentExecution
        from backend.app.modules.assistant.control import PRELAUNCH_STOP
        points = [(row.root_work_id, AssistantWorkAuthority, AssistantDispatch)]
        if row.parent_id != row.root_work_id:
            points.append((row.parent_id, AssistantTurnAuthority, AssistantTurnDispatch))
        for identity, grant_model, intent_model in points:
            grant = db.get(grant_model, identity, populate_existing=True)
            intent = db.get(intent_model, identity, populate_existing=True)
            execution = db.get(AgentExecution, intent.run_id, populate_existing=True) if intent else None
            stopped = [grant.revoked_at if grant else None,
                       intent.finished_at if intent and intent.code == PRELAUNCH_STOP else None,
                       execution.cancel_requested_at if execution else None]
            if any(at is not None and _utc(at) >= _utc(row.issued_at) for at in stopped):
                fail('assistant_inbox_authority_denied', 403)
        origin = db.get(LoginSession, row.origin_session_id, populate_existing=True)
        account = db.get(EmployeeAccount, row.account_id, populate_existing=True)
        org, store, person, roles = load_scope(db, row.organization_id, row.store_id, row.owner_id)
        epoch = db.scalar(select(ScopeSecurityFence.work_version).where(ScopeSecurityFence.organization_id==row.organization_id,
                          ScopeSecurityFence.store_id==row.store_id))
        if (origin is None or (origin.account_id, origin.account_version) != (row.account_id, row.account_version)
                or account is None or not account.active or account.version != row.account_version
                or (account.organization_id, account.store_id, account.subject_id) != (row.organization_id, row.store_id, row.owner_id)
                or account.locked_until is not None and _utc(account.locked_until) > instant
                or epoch != row.work_epoch or org is None or not org.active or store is None or not store.active
                or person is None or not person.active or person.subject_type != 'person'
                or store.organization_id != row.organization_id or person.organization_id != row.organization_id):
            fail('assistant_inbox_authority_denied', 403)
        for key in REQUIRED | {permission}:
            if key not in row.permissions or not permission_allowed(grants_for(db,row.organization_id,row.store_id,row.owner_id,roles,key),row.owner_id,now=instant):
                fail('assistant_inbox_authority_denied', 403)
        return tuple(row.permissions)


def cancel_pending(db, actor, root_work_id):
    """Same human stop transaction; includes messages received after an old stop."""
    from backend.app.modules.assistant.inbox_execution import converted
    lock_command_authority(db, actor); _human(db, actor, write=True)
    service.read_work(db, actor, root_work_id)
    rows = list(db.scalars(select(AssistantInbox).where(*_scope(actor),
        AssistantInbox.root_work_id == root_work_id, AssistantInbox.cancelled_at.is_(None))
        .order_by(AssistantInbox.sequence)))
    cancelled = []
    for row in rows:
        if converted(db, row) is None:
            cancel(db, actor, row.id); cancelled.append(row.id)
    return cancelled


def stop_message(session, actor, message_id):
    """Owns commit. Converted messages use the existing exact-Run stop protocol."""
    from backend.app.modules.assistant.inbox_execution import converted
    from backend.app.modules.assistant.turn_execution import stop_turn
    lock_command_authority(session, actor); _human(session, actor, write=True)
    row = session.scalar(select(AssistantInbox).where(*_scope(actor), AssistantInbox.id == message_id))
    if row is None: fail('assistant_inbox_not_found', 404)
    _verify(session, row); turn = converted(session, row)
    if turn is None:
        cancel(session, actor, message_id); session.commit()
        outcome = 'stopped_before_conversion'
    else:
        outcome = stop_turn(session, actor, row.root_work_id, turn.id)['stop_result']
    return {'message':_full_view(session, actor, row), 'stop_result':outcome}


def read_message(db, actor, root_work_id, message_id):
    _human(db, actor)
    row = db.scalar(select(AssistantInbox).where(*_scope(actor), AssistantInbox.root_work_id == root_work_id,
                                                AssistantInbox.id == message_id))
    if row is None: fail('assistant_inbox_not_found', 404)
    return _full_view(db, actor, _verify(db, row))


def conversation_snapshot(db, actor, root_work_id):
    """Caller owns commit; one serialized read of receipt/turn conversion."""
    epoch = lock_command_authority(db, actor); _human(db, actor)
    root = service.read_work(db, actor, root_work_id)
    chain = turns.list_turns(db, actor, root_work_id)
    messages = list_messages(db, actor, root_work_id)
    def final(session, instant):
        for permission in ('agents.run', 'tasks.read_self'):
            check_command_authority(session, actor, permission=permission, epoch=epoch, now=instant)
    commit_guard.enlist(db, action='assistant.conversation_read', check=final)
    return {'root':root, 'turns':chain, 'messages':messages}
