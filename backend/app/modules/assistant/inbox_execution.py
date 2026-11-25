"""Convert finite inbox consent to exactly one ordinary Pi turn, atomically.

No bearer reconstruction, model call, parent-result invention, or retry here.
The existing exact-turn worker owns launch, effects, receipts and recovery.
"""
from datetime import datetime
from fastapi import HTTPException
from sqlalchemy import select, update

from backend.app.application import commit_guard
from backend.app.modules.assistant import inbox, turns, authority
from backend.app.modules.assistant.models import (
    AssistantInbox, AssistantTurn, AssistantTurnDispatch, AssistantTurnAuthority)
from backend.app.modules.assistant.principal import CAPABILITIES
from backend.app.modules.assistant.turn_adapter import DshTurnsAdapter
from backend.app.modules.audit.service import record_event
from backend.app.modules.auth.models import EmployeeAccount
from backend.app.modules.identity.command_guard import lock_work_security_fence
from backend.app.modules.identity.models import Store
from backend.app.modules.identity.repository import load_scope, grants_for
from backend.app.modules.identity.service import permission_allowed
from backend.app.workers.outbox import enqueue


def _lineage(db, message, turn):
    """A queued message may follow earlier queued messages, never a manual fork."""
    prior = list(db.scalars(select(AssistantTurn).where(AssistantTurn.root_work_id == message.root_work_id,
        AssistantTurn.sequence < turn.sequence).order_by(AssistantTurn.sequence)))
    ids = [message.root_work_id] + [row.id for row in prior]
    if message.parent_id not in ids: inbox.fail('assistant_inbox_parent_changed')
    after = prior[ids.index(message.parent_id):]
    last_sequence = 0
    for row in after:
        origin = db.get(AssistantInbox, row.source_inbox_id) if row.source_inbox_id else None
        if (origin is None or origin.root_work_id != message.root_work_id
                or not last_sequence < origin.sequence < message.sequence
                or row.source_inbox_sha256 != origin.binding_sha256):
            inbox.fail('assistant_inbox_parent_changed')
        last_sequence = origin.sequence
    if turn.previous_turn_id != ids[-1]: inbox.fail('assistant_inbox_parent_changed')


def verify_origin(db, turn, *, grant=None):
    """Integrity only, including terminal readback. Missing provenance is fatal."""
    receipt = db.scalar(select(AssistantInbox).where(
        AssistantInbox.organization_id == turn.organization_id, AssistantInbox.store_id == turn.store_id,
        AssistantInbox.owner_id == turn.owner_id, AssistantInbox.request_id == turn.request_id)
        .execution_options(populate_existing=True))
    source = (turn.source_inbox_id, turn.source_inbox_sha256)
    if source == (None, None):
        if receipt is not None or grant is not None and (grant.source_inbox_id, grant.source_inbox_sha256) != source:
            inbox.fail()
        return
    if (receipt is None or source != (receipt.id, receipt.binding_sha256)
            or (turn.root_work_id, turn.origin_account_id, turn.text, turn.request_digest) !=
               (receipt.root_work_id, receipt.account_id, receipt.text, receipt.request_digest)
            or authority._utc(turn.created_at) < authority._utc(receipt.issued_at)):
        inbox.fail()
    inbox._verify(db, receipt)
    _lineage(db, receipt, turn)
    # Conversion order is part of the immutable source, not just scheduler intent.
    for prior in db.scalars(select(AssistantInbox).where(AssistantInbox.root_work_id == receipt.root_work_id,
            AssistantInbox.sequence < receipt.sequence)):
        previous_turn = db.scalar(select(AssistantTurn).where(AssistantTurn.source_inbox_id == prior.id))
        if previous_turn is not None:
            if (previous_turn.sequence >= turn.sequence or previous_turn.source_inbox_sha256 != prior.binding_sha256
                    or prior.binding_sha256 != inbox._seal(prior)): inbox.fail()
        elif prior.cancelled_at is None or authority._utc(prior.cancelled_at) > authority._utc(turn.created_at):
            inbox.fail('assistant_inbox_order_conflict')
    if grant is not None and (
            (grant.source_inbox_id, grant.source_inbox_sha256) != source
            or (grant.origin_session_id, grant.account_version, grant.work_epoch) !=
               (receipt.origin_session_id, receipt.account_version, receipt.work_epoch)
            or not set(grant.permissions) <= set(receipt.permissions)
            or not authority._utc(receipt.issued_at) <= authority._utc(grant.issued_at)
                   < authority._utc(grant.expires_at) <= authority._utc(receipt.expires_at)):
        inbox.fail()


def converted(db, message):
    """Read unique conversion by both marker and request id; do not infer newness."""
    by_source = db.scalar(select(AssistantTurn).where(AssistantTurn.source_inbox_id == message.id))
    by_request = db.scalar(select(AssistantTurn).where(AssistantTurn.organization_id == message.organization_id,
        AssistantTurn.store_id == message.store_id, AssistantTurn.owner_id == message.owner_id,
        AssistantTurn.request_id == message.request_id))
    if by_source is None and by_request is None: return None
    if by_source is None or by_request is None or by_source.id != by_request.id: inbox.fail()
    verify_origin(db, by_source)
    grant = db.get(AssistantTurnAuthority, by_source.id, populate_existing=True)
    intent = db.get(AssistantTurnDispatch, by_source.id, populate_existing=True)
    if grant is None or intent is None: inbox.fail()
    authority._bindings(db, grant)
    return by_source


def _pending(db, scope, root_id):
    for row in db.scalars(select(AssistantInbox).where(*inbox._scope(scope), AssistantInbox.root_work_id == root_id)
                          .order_by(AssistantInbox.sequence)):
        inbox._verify(db, row)
        turn = converted(db, row)
        if turn is None and row.cancelled_at is None: return row
    return None


def _policy(config):
    from backend.app.modules.assistant.dispatch import _config
    _config(config)
    policy = config.inbox_policy
    if policy is None: inbox.fail('assistant_inbox_execution_disabled', 503)
    return policy


def convert_next(db, config, *, root_work_id):
    """Caller owns commit. Authority derives from the receipt and current Host."""
    from backend.app.modules.assistant import dispatch, turn_execution
    policy = _policy(config)
    machine, now = dispatch._machine(db, config)
    # Same lock order as human intake/stop and exact-work execution.
    lock_work_security_fence(db, config.pi.organization_id, config.pi.store_id)
    changed = db.execute(update(Store).where(Store.organization_id == config.pi.organization_id,
        Store.id == config.pi.store_id, Store.active.is_(True)).values(active=Store.active)
        .execution_options(synchronize_session=False))
    if changed.rowcount != 1: inbox.fail('assistant_inbox_authority_denied', 403)
    from backend.app.modules.assistant.models import AssistantWork
    work = db.get(AssistantWork, root_work_id, populate_existing=True)
    if (work is None or (work.organization_id, work.store_id) != (config.pi.organization_id, config.pi.store_id)
            or config.online_policy is not None and work.owner_id != config.online_policy.requester_id):
        inbox.fail('assistant_work_not_found', 404)
    actor = inbox.Scope(work.organization_id, work.store_id, work.owner_id)
    receipt = _pending(db, actor, root_work_id)
    if receipt is None: return None
    db.scalar(select(EmployeeAccount.id).where(EmployeeAccount.id == receipt.account_id).with_for_update())
    expected_receipt = receipt.binding_sha256
    inbox.check_authority(db, receipt.id, expected_receipt, now=now)
    expires = min(authority._utc(receipt.expires_at), authority._utc(policy.expires_at))
    if expires <= now: inbox.fail('assistant_inbox_authority_denied', 403)
    _, _, _, roles = load_scope(db, work.organization_id, work.store_id, work.owner_id)
    permissions = sorted(set(receipt.permissions).intersection(policy.permissions, CAPABILITIES))
    permissions = [key for key in permissions if permission_allowed(grants_for(db,work.organization_id,
        work.store_id,work.owner_id,roles,key),work.owner_id,now=now)]
    if not inbox.REQUIRED <= set(permissions): inbox.fail('assistant_inbox_authority_denied', 403)
    work, view, _ = turns._root(db, actor, root_work_id, require_active=True)
    latest = db.scalar(select(AssistantTurn).where(*turns._scope(actor), AssistantTurn.root_work_id == root_work_id)
                       .order_by(AssistantTurn.sequence.desc()).limit(1))
    previous = latest.id if latest else root_work_id
    fact = turns._parent_fact(db, actor, root_work_id, previous)
    turn = turns._build_turn(db, actor, work, view, latest, fact, text=receipt.text,
        request_id=receipt.request_id, core=config.turns, origin_account_id=receipt.account_id)
    turn.source_inbox_id, turn.source_inbox_sha256 = receipt.id, expected_receipt
    _lineage(db, receipt, turn)
    event = enqueue(db, organization_id=work.organization_id, store_id=work.store_id,
        event_type=turn_execution.EVENT_TYPE, idempotency_key='assistant-turn:'+turn.id,
        payload=turn_execution.payload(turn), now=turn.created_at)
    intent = AssistantTurnDispatch(work_id=turn.id, run_id=turn.run_id, session_id=receipt.origin_session_id,
        account_version=receipt.account_version, authorization_expires_at=expires,
        state='pending', outbox_event_id=event.id)
    db.add(turn); db.flush([turn]); db.add(intent); db.flush([intent])
    grant = AssistantTurnAuthority(work_id=turn.id, run_id=turn.run_id,
        organization_id=work.organization_id, store_id=work.store_id, owner_id=work.owner_id,
        account_id=receipt.account_id, origin_session_id=receipt.origin_session_id,
        account_version=receipt.account_version, work_epoch=receipt.work_epoch,
        request_id=turn.request_id, request_digest=turn.request_digest, snapshot_sha256=turn.snapshot_sha256,
        permissions=permissions, issued_at=turn.created_at, expires_at=expires,
        source_inbox_id=receipt.id, source_inbox_sha256=expected_receipt)
    grant.binding_sha256 = authority._seal(grant); db.add(grant); db.flush()
    expected_turn, expected_grant = turns._seal(turn), grant.binding_sha256
    def final(session, instant):
        inbox.check_authority(session, receipt.id, expected_receipt, now=instant)
        session.refresh(turn); session.refresh(grant)
        if turns._seal(turn) != expected_turn or grant.binding_sha256 != expected_grant: inbox.fail()
        turns._validate(session, actor, turn, require_active=True)
        tail = session.scalar(select(AssistantTurn.id).where(AssistantTurn.root_work_id == root_work_id)
                              .order_by(AssistantTurn.sequence.desc()).limit(1))
        if tail != turn.id: inbox.fail('assistant_inbox_parent_changed')
        # Every granted permission must still be permitted at the final clock.
        for permission in permissions:
            authority._validate(session, turn.id, turn.run_id, expected_grant, permission, instant, kind='turn')
    commit_guard.enlist(db, action='assistant.inbox_converted', check=final)
    dispatch._machine_commit(db, config)
    record_event(db, actor_id=machine.subject_id, organization_id=work.organization_id, store_id=work.store_id,
        action='assistant.inbox_converted', target_type='assistant_inbox', target_id=receipt.id,
        idempotency_key='assistant-inbox-convert:'+receipt.id,
        details={'root_work_id':root_work_id,'turn_id':turn.id,'binding_sha256':expected_receipt,
                 'grant_sha256':expected_grant}, occurred_at=turn.created_at)
    return {'message_id':receipt.id, 'turn_id':turn.id, 'run_id':turn.run_id}


def tick(config):
    """At most one conversion; a deferred root must not starve other roots."""
    from backend.app.modules.assistant import dispatch
    _policy(config)
    with config.pi.session_factory() as db:
        dispatch._machine(db, config)
        owner = [AssistantInbox.owner_id == config.online_policy.requester_id] if config.online_policy else []
        roots = list(db.scalars(select(AssistantInbox.root_work_id).where(
            AssistantInbox.organization_id == config.pi.organization_id,
            AssistantInbox.store_id == config.pi.store_id, *owner,
            AssistantInbox.cancelled_at.is_(None),
            ~select(AssistantTurn.id).where(AssistantTurn.source_inbox_id == AssistantInbox.id).exists())
            .group_by(AssistantInbox.root_work_id).order_by(AssistantInbox.root_work_id)))
    waiting = False
    for root_id in roots:
        try:
            with config.pi.session_factory.begin() as db: result = convert_next(db, config, root_work_id=root_id)
            if result is not None: return 'converted'
        except HTTPException as error:
            # Machine/security-infrastructure failure still stops the worker.
            # User/source/parent constraints retain the receipt, never replay.
            if not str(error.detail).startswith(('assistant_inbox_', 'assistant_followup_',
                                                  'assistant_work_', 'assistant_turn_')): raise
            waiting = True
    return 'inbox_waiting' if waiting else 'idle'
