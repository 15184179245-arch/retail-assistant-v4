"""Host-only work authority. No login token, HTTP input or runtime-frame authority.

Root work and followup turns have separate exact-run records and markers.
Only explicit Host intake issues authority. Approvals require a current human session.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import select, update

from backend.app.application import commit_guard
from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.assistant.core_adapter import digest
from backend.app.modules.assistant.models import (AssistantWork, AssistantDispatch, AssistantWorkAuthority,
    AssistantTurn, AssistantTurnDispatch, AssistantTurnAuthority)
from backend.app.modules.auth.models import EmployeeAccount, LoginSession
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.command_guard import (
    lock_command_authority, check_command_authority, lock_work_security_fence,
    refresh_authority, ScopeSecurityFenceError,
)
from backend.app.modules.identity.models import Store, ScopeSecurityFence
from backend.app.modules.identity.repository import load_scope, grants_for
from backend.app.modules.identity.service import authorize_scope, permission_allowed

_REQUIRED = ('agents.run', 'tasks.manage', 'tasks.read_self')


def _deny(code='assistant_work_authority_denied'):
    raise HTTPException(403, code)


def _models(kind):
    if kind == 'work':return AssistantWork, AssistantDispatch, AssistantWorkAuthority
    if kind == 'turn':return AssistantTurn, AssistantTurnDispatch, AssistantTurnAuthority
    _deny('assistant_work_principal_denied')


def authority_model(kind):
    return _models(kind)[2]


def row_kind(row):
    if type(row) is AssistantWorkAuthority:return 'work'
    if type(row) is AssistantTurnAuthority:return 'turn'
    _deny('assistant_work_principal_denied')


def _utc(value):
    if not isinstance(value, datetime): _deny()
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@dataclass(frozen=True)
class WorkAuthorityPolicy:
    # Passed only by a trusted Host, never from a browser/agent request. The Host
    # must use its real policy cutoff; this record cannot extend model budgets.
    expires_at: datetime
    permissions: tuple[str, ...]


@dataclass(frozen=True)
class WorkAuthoritySnapshot:
    work_id: str
    run_id: str
    organization_id: str
    store_id: str
    owner_id: str
    account_id: str
    account_version: int
    work_epoch: int
    expires_at: datetime
    permissions: tuple[str, ...]
    binding_sha256: str


def _seal(row):
    binding = {key: getattr(row, key) for key in (
        'work_id', 'run_id', 'organization_id', 'store_id', 'owner_id', 'account_id',
        'origin_session_id', 'account_version', 'work_epoch', 'request_id', 'request_digest',
        'snapshot_sha256', 'permissions')} | {
        'issued_at': _utc(row.issued_at).isoformat(), 'expires_at': _utc(row.expires_at).isoformat()}
    if row_kind(row) == 'turn':
        binding.update(kind='turn', version=2)
        if row.source_inbox_id is not None or row.source_inbox_sha256 is not None:
            binding.update(version=3, source_inbox_id=row.source_inbox_id,
                           source_inbox_sha256=row.source_inbox_sha256)
    pin = getattr(row, 'workspace_binding_sha256', None)
    if pin is not None:
        if type(pin) is not str or len(pin) != 64 or any(c not in '0123456789abcdef' for c in pin):
            _deny('assistant_work_authority_binding_conflict')
        binding['workspace_binding_sha256'] = pin
    return digest(binding)


def _snapshot(row):
    return WorkAuthoritySnapshot(row.work_id, row.run_id, row.organization_id, row.store_id,
        row.owner_id, row.account_id, row.account_version, row.work_epoch, _utc(row.expires_at),
        tuple(row.permissions), row.binding_sha256)


def _bindings(session, row):
    from backend.app.modules.assistant.service import _view
    if (type(row.permissions) is not list or not row.permissions
            or any(type(p) is not str for p in row.permissions)
            or row.permissions != sorted(set(row.permissions))
            or not set(_REQUIRED) <= set(row.permissions)
            or _seal(row) != row.binding_sha256):
        _deny('assistant_work_authority_binding_conflict')
    work_model, dispatch_model, _ = _models(row_kind(row))
    work = session.get(work_model, row.work_id, populate_existing=True)
    dispatch = session.get(dispatch_model, row.work_id, populate_existing=True)
    origin = session.get(LoginSession, row.origin_session_id, populate_existing=True)
    if (work is None or dispatch is None or origin is None
            or (work.organization_id, work.store_id, work.owner_id, work.origin_account_id,
                work.request_id, work.request_digest, work.snapshot_sha256) != (
                row.organization_id, row.store_id, row.owner_id, row.account_id,
                row.request_id, row.request_digest, row.snapshot_sha256)
            or (dispatch.run_id, dispatch.session_id, dispatch.account_version) != (
                row.run_id, row.origin_session_id, row.account_version)
            or (origin.account_id, origin.account_version) != (row.account_id, row.account_version)):
        _deny('assistant_work_authority_binding_conflict')
    # Read-only integrity validation, not a fabricated authenticated identity.
    # _view without execution only compares work/Task/outbox source bindings.
    @dataclass(frozen=True)
    class Scope:
        organization_id: str
        store_id: str
        subject_id: str
    if row_kind(row) == 'turn':
        from backend.app.modules.assistant.turn_execution import binding
        from backend.app.modules.assistant.inbox_execution import verify_origin
        verify_origin(session, work, grant=row)
        view = binding(session, work, dispatch)
    else:
        view = _view(session, Scope(row.organization_id, row.store_id, row.owner_id), work, include_execution=False)
    from backend.app.modules.assistant.workspace_binding import validate_origin
    validate_origin(session, row)
    return dispatch, view


def _validate(session, work_id, run_id, binding_sha256, permission, now, kind='work', *, lock=True):
    """Read-only check under existing locks, also safe for commit_guard."""
    with session.no_autoflush:
        refresh_authority(session)
        row = session.get(authority_model(kind), work_id, populate_existing=True)
        if (row is None or row.run_id != run_id or row.binding_sha256 != binding_sha256
                or row.revoked_at is not None or not _utc(row.issued_at) <= now < _utc(row.expires_at)):
            _deny()
        dispatch, view = _bindings(session, row)
        if row.workspace_binding_sha256 is not None:
            from backend.app.modules.assistant.workspace_binding import validate_origin, current_thread
            current_thread(session, validate_origin(session, row), lock=lock)
        if kind == 'turn' and row.source_inbox_id is not None:
            from backend.app.modules.assistant.inbox import check_authority
            check_authority(session, row.source_inbox_id, row.source_inbox_sha256,
                            permission=permission, now=now)
        if view['task_state'] not in ('accepted', 'assigned', 'in_progress'): _deny()
        if dispatch.state not in ('pending', 'launching') or permission not in row.permissions:
            _deny()
        epoch = session.scalar(select(ScopeSecurityFence.work_version).where(
            ScopeSecurityFence.organization_id == row.organization_id,
            ScopeSecurityFence.store_id == row.store_id))
        if epoch != row.work_epoch: _deny()
        account = session.get(EmployeeAccount, row.account_id, populate_existing=True)
        org, store, person, roles = load_scope(session, row.organization_id, row.store_id, row.owner_id)
        if (account is None or not account.active or account.version != row.account_version
                or (account.organization_id, account.store_id, account.subject_id) != (
                    row.organization_id, row.store_id, row.owner_id)
                or (account.locked_until is not None and _utc(account.locked_until) > now)
                or org is None or not org.active or store is None or not store.active
                or person is None or not person.active or person.subject_type != 'person'
                or store.organization_id != row.organization_id or person.organization_id != row.organization_id):
            _deny()
        for key in set(_REQUIRED) | {permission}:
            if key not in row.permissions or not permission_allowed(grants_for(
                    session, row.organization_id, row.store_id, row.owner_id, roles, key), row.owner_id, now=now):
                _deny()
        return _snapshot(row)


def issue_work_authority(session, actor, work, dispatch, policy, *, kind='work'):
    """Only the new-intake transaction may issue. Never backfill or renew work."""
    from backend.app.modules.identity.permission_admin import KNOWN_PERMISSIONS
    work_model, dispatch_model, grant_model = _models(kind)
    if (type(work) is not work_model or type(dispatch) is not dispatch_model
            or type(actor) is not AuthenticatedSubject or type(policy) is not WorkAuthorityPolicy
            or type(policy.expires_at) is not datetime or policy.expires_at.tzinfo is None
            or policy.expires_at.utcoffset() is None
            or type(policy.permissions) is not tuple or not policy.permissions
            or any(type(p) is not str for p in policy.permissions)
            or len(set(policy.permissions)) != len(policy.permissions)
            or not set(_REQUIRED) <= set(policy.permissions)
            or not set(policy.permissions) <= KNOWN_PERMISSIONS
            or work not in session.new or dispatch not in session.new or dispatch.state != 'pending'
            or dispatch.work_id != work.task_id
            or (work.organization_id, work.store_id, work.owner_id, work.origin_account_id) != (
                actor.organization_id, actor.store_id, actor.subject_id, actor.account_id)
            or (dispatch.session_id, dispatch.account_version) != (actor.session_id, actor.account_version)):
        _deny('assistant_work_authority_issuance_denied')
    from backend.app.modules.assistant.workspace_binding import root_context, augment_policy, context_sha256
    context = root_context(session, work, kind=kind)
    policy = augment_policy(session, actor, policy, context)
    epoch = lock_command_authority(session, actor)
    now = commit_guard._db_clock(session)
    if _utc(policy.expires_at) <= now: _deny()
    # Work and dispatch must both still be pending in this exact intake root.
    # Never infer newness from timestamps or an absent legacy dispatch row.
    with session.no_autoflush:
        for permission in policy.permissions:
            authorize_scope(session, actor, organization_id=actor.organization_id, store_id=actor.store_id,
                            permission=permission, now=now)
        if session.get(grant_model, work.task_id) is not None: _deny()
        work_epoch = lock_work_security_fence(session, actor.organization_id, actor.store_id)
    session.flush([work])
    session.flush([dispatch])
    row = grant_model(work_id=work.task_id, run_id=dispatch.run_id,
        organization_id=work.organization_id, store_id=work.store_id, owner_id=work.owner_id,
        account_id=actor.account_id, origin_session_id=actor.session_id, account_version=actor.account_version,
        work_epoch=work_epoch, request_id=work.request_id, request_digest=work.request_digest, snapshot_sha256=work.snapshot_sha256,
        permissions=sorted(policy.permissions), issued_at=now, expires_at=_utc(policy.expires_at))
    row.workspace_binding_sha256 = context_sha256(context)
    row.binding_sha256 = _seal(row)
    session.add(row)
    expected = _snapshot(row)
    def final(db, instant):
        for permission in expected.permissions:
            check_command_authority(db, actor, permission=permission, epoch=epoch, now=instant)
        _validate(db, expected.work_id, expected.run_id, expected.binding_sha256, 'agents.run', instant, kind=kind, lock=False)
    commit_guard.enlist(session, action='assistant.work_authority_issued', check=final)
    record_event(session, actor_id=actor.subject_id, organization_id=actor.organization_id,
        store_id=actor.store_id, action='assistant.work_authority_issued', target_type='assistant_turn' if kind == 'turn' else 'task',
        target_id=work.task_id, idempotency_key='work-authority-issue:' + work.task_id,
        details={'binding_sha256': row.binding_sha256, 'run_id': row.run_id}, occurred_at=now)
    return expected


def lock_work_authority(session, *, work_id, run_id, binding_sha256, permission, kind='work'):
    """Internal execution-boundary primitive; not accepted by HTTP auth/approval.

    Caller owns the transaction. Returns a frozen, non-credential snapshot and
    installs a commit recheck at each participating execution boundary.
    """
    with session.no_autoflush:
        if any(isinstance(r, (AssistantWorkAuthority, AssistantWork, AssistantDispatch, AssistantTurnAuthority, AssistantTurn, AssistantTurnDispatch))
               for r in (*session.new, *session.dirty, *session.deleted)):
            _deny('assistant_work_authority_changes_pending')
        refresh_authority(session)
        row = session.get(authority_model(kind), work_id, populate_existing=True)
        if row is None or row.run_id != run_id or row.binding_sha256 != binding_sha256: _deny()
        # The first read is just a locator. Re-read every binding after locks.
        scope = (row.organization_id, row.store_id, row.account_id)
        try: lock_work_security_fence(session, scope[0], scope[1])
        except ScopeSecurityFenceError: _deny()
        changed = session.execute(update(Store).where(Store.organization_id == scope[0],
            Store.id == scope[1], Store.active.is_(True)).values(active=Store.active)
            .execution_options(synchronize_session=False))
        if changed.rowcount != 1: _deny()
        session.scalar(select(EmployeeAccount.id).where(EmployeeAccount.id == scope[2]).with_for_update())
        row = session.get(authority_model(kind), work_id, populate_existing=True)
        if row is None or (row.organization_id, row.store_id, row.account_id) != scope: _deny()
        result = _validate(session, work_id, run_id, binding_sha256, permission, commit_guard._db_clock(session), kind=kind)
        def final(db, instant):
            _validate(db, work_id, run_id, binding_sha256, permission, instant, kind=kind, lock=False)
        commit_guard.enlist(session, action='assistant.work_authority_used', check=final)
        return result


def revoke_work_authority(session, actor, work_id, *, kind='work'):
    """Current owner stop transaction; no reactivation, even after a new login."""
    epoch = lock_command_authority(session, actor)
    now = commit_guard._db_clock(session)
    for key in _REQUIRED:
        authorize_scope(session, actor, organization_id=actor.organization_id, store_id=actor.store_id,
                        permission=key, now=now)
    row = session.get(authority_model(kind), work_id, populate_existing=True)
    if row is None: return False  # Legacy work has no authority to revoke.
    if (row.organization_id, row.store_id, row.owner_id, row.account_id) != (
            actor.organization_id, actor.store_id, actor.subject_id, actor.account_id):
        _deny()
    _bindings(session, row)
    def final(db, instant):
        for key in _REQUIRED:
            check_command_authority(db, actor, permission=key, epoch=epoch, now=instant)
    commit_guard.enlist(session, action='assistant.work_authority_revoked', check=final)
    if row.revoked_at is not None: return False
    row.revoked_at = now
    record_event(session, actor_id=actor.subject_id, organization_id=actor.organization_id,
        store_id=actor.store_id, action='assistant.work_authority_revoked', target_type='assistant_turn' if kind == 'turn' else 'task',
        target_id=work_id, idempotency_key='work-authority-revoke:' + work_id,
        details={'binding_sha256': row.binding_sha256}, occurred_at=now)
    return True
