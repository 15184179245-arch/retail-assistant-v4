"""Durable organization/store security fence and transaction locks for commands.

Lock order (AGT-026 B1): ScopeSecurityFence -> Store -> EmployeeAccount ->
LoginSession -> AgentExecution -> AgentRun -> step/confirmation -> domain rows.

The fence row is the durable authorization epoch of one organization/store
scope. Revocation-capable changes (logout, account invalidation, account
lockout, personal permission change, machine credential change) take the
exclusive row lock and increment ``version``; read-only command guards take the
shared row lock, so they serialize against those changes. The epoch only ever
moves forward and is never a permission source. The separate work_version
shares the same physical lock and advances on all security changes except
ordinary single-session logout. These locks grant nothing, and
callers MUST revalidate the account, session and action permission after
acquiring them, inside the same transaction that reaches the caller's commit.

Callers MUST take the fence before staging an authorization change (revocation,
grant edit, machine credential change) and keep one transaction through their
commit; these helpers never flush or commit on their own. A store that only
exists as a pending ORM object must be persisted by the caller before it can
carry a scope.

PostgreSQL uses a shared/exclusive row lock on the fence row. SQLite has no
shared row lock, so every guard there takes its conservative database write
lock instead (a no-op UPDATE on the fence row, taken even before first-use
provisioning); that serializes purely local commands and does not claim the
PostgreSQL concurrency guarantee.
"""
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.auth.models import EmployeeAccount, LoginSession
from backend.app.modules.identity.models import (
    Organization,
    PermissionGrant,
    RoleAssignment,
    ScopeSecurityFence,
    Store,
    Subject,
)

DEFAULT_SCOPE_EPOCH = 1
_AUTHORITY_MODELS = (Organization, Store, Subject, PermissionGrant, RoleAssignment, EmployeeAccount, LoginSession)


def refresh_authority(session: Session) -> None:
    """Refresh cached authorization facts without flushing/expiring domain work.

    Mixing unflushed ACL edits into a business guard would overwrite them or
    authorize from an uncommitted policy. Authorization editors take their own
    exclusive fence before staging those edits instead.
    """
    if any(isinstance(row, _AUTHORITY_MODELS)
           for row in (*session.new, *session.dirty, *session.deleted)):
        raise HTTPException(403, "authority_changes_pending")
    for row in tuple(session.identity_map.values()):
        if isinstance(row, _AUTHORITY_MODELS):
            session.expire(row)


def _check_command_account_unlocked(session: Session, actor: AuthenticatedSubject, *, now: datetime) -> None:
    """Command policy only; existing bearer/read authentication stays unchanged."""
    if actor.account_id is None and actor.session_id is None:
        return  # Preserve the existing sessionless internal-identity policy.
    from backend.app.core.security import ensure_subject_active
    from backend.app.modules.auth.service import validate_actor_session

    with session.no_autoflush:
        # Keep identity/session expiry ahead of the additional command denial.
        # The callers refreshed authorization rows under their original locks.
        ensure_subject_active(actor, now)
        account = validate_actor_session(session, actor, now=now)
        if account.locked_until is not None and _utc(account.locked_until) > _utc(now):
            raise HTTPException(403, "permission_denied")


def lock_command_authority(session: Session, actor: AuthenticatedSubject) -> int:
    """Lock a command scope; preserve sessionless internal domain identities."""
    from backend.app.modules.assistant.principal import WorkPrincipal, check
    if type(actor) is WorkPrincipal:
        check(session, actor, lock=True)
        return actor.work_epoch
    with session.no_autoflush:
        refresh_authority(session)
        if actor.account_id is not None or actor.session_id is not None:
            lock_employee_authority(session, actor)
            from backend.app.application.commit_guard import _db_clock
            _check_command_account_unlocked(session, actor, now=_db_clock(session))
        else:
            try:
                lock_scope_security_fence(session, actor.organization_id, actor.store_id)
            except ScopeSecurityFenceError as error:
                raise HTTPException(403, "store_unavailable") from error
            changed = session.execute(update(Store).where(
                Store.organization_id == actor.organization_id, Store.id == actor.store_id,
                Store.active.is_(True),
            ).values(active=Store.active).execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise HTTPException(403, "store_unavailable")
            refresh_authority(session)
        return session.scalar(select(ScopeSecurityFence.version).where(
            *_scope_columns(actor.organization_id, actor.store_id)))


def check_command_authority(session: Session, actor: AuthenticatedSubject, *,
                            permission: str, epoch: int, now: datetime) -> None:
    """Read-only final check under the original command locks, never relock."""
    from backend.app.modules.identity.service import authorize_scope
    from backend.app.modules.assistant.principal import WorkPrincipal, check
    if type(actor) is WorkPrincipal:
        if type(epoch) is not int or epoch != actor.work_epoch:
            raise HTTPException(403, 'permission_denied')
        check(session, actor, permission=permission, now=now)
        return
    with session.no_autoflush:
        refresh_authority(session)
        current = session.scalar(select(ScopeSecurityFence.version).where(
            *_scope_columns(actor.organization_id, actor.store_id)))
        if current != epoch:
            raise HTTPException(403, "permission_denied")
        authorize_scope(session, actor, organization_id=actor.organization_id,
                        store_id=actor.store_id, permission=permission, now=now)
        _check_command_account_unlocked(session, actor, now=now)


class ScopeSecurityFenceError(RuntimeError):
    """The requested fence scope or epoch cannot be honoured; callers fail shut."""


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _require_scope_store(session: Session, organization_id: str, store_id: str) -> None:
    found = session.scalar(select(Store.id).where(
        Store.organization_id == organization_id,
        Store.id == store_id,
    ))
    if found is None:
        raise ScopeSecurityFenceError("security fence scope has no store in its organization")


def _provision_scope_fence(session: Session, organization_id: str, store_id: str, now: datetime) -> None:
    """Create the scope row once when the migration did not seed it.

    Production rows come from migration 0041. This path keeps databases created
    by ``Base.metadata.create_all`` (integration fixtures) and scopes added
    after the migration usable without a second write from the caller.
    """
    scope = _scope_columns(organization_id, store_id)
    exists = session.scalar(select(ScopeSecurityFence.version).where(*scope))
    if exists is not None:
        return
    _require_scope_store(session, organization_id, store_id)
    values = {
        "organization_id": organization_id,
        "store_id": store_id,
        "version": DEFAULT_SCOPE_EPOCH,
        "work_version": DEFAULT_SCOPE_EPOCH,
        "updated_at": now,
    }
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        dialect_insert = None
    if dialect_insert is None:
        session.execute(insert(ScopeSecurityFence).values(**values))
        return
    # A concurrent transaction may insert the same scope first; the row lock
    # acquired right below decides which epoch every caller then observes.
    session.execute(
        dialect_insert(ScopeSecurityFence)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["organization_id", "store_id"])
    )


def _scope_columns(organization_id: str, store_id: str) -> tuple:
    return (
        ScopeSecurityFence.organization_id == organization_id,
        ScopeSecurityFence.store_id == store_id,
    )


def _touch_scope_fence(session: Session, organization_id: str, store_id: str):
    """No-op write on the fence row: the conservative SQLite transaction lock."""
    return session.execute(update(ScopeSecurityFence).where(
        *_scope_columns(organization_id, store_id),
    ).values(version=ScopeSecurityFence.version).execution_options(synchronize_session=False))


def lock_scope_security_fence(
    session: Session,
    organization_id: str,
    store_id: str,
    *,
    exclusive: bool = False,
    expected_epoch: int | None = None,
    now: datetime | None = None,
) -> int:
    """Take the shared (read) or exclusive (write) lock on one scope fence.

    Returns the epoch observed under the lock. ``expected_epoch`` makes the lock
    fail shut when a revocation already moved the epoch in another transaction.
    """
    if not organization_id or not store_id:
        raise ScopeSecurityFenceError("security fence requires an organization and store scope")
    if expected_epoch is not None and (type(expected_epoch) is not int or expected_epoch <= 0):
        raise ScopeSecurityFenceError("security fence expected_epoch must be a positive integer")
    current = _utc(now)
    scope = _scope_columns(organization_id, store_id)
    # Never autoflush here: a caller's pending authorization change must reach
    # the database only through the fence, and a store the caller merely created
    # in the session has to be persisted by the caller before this scope exists.
    with session.no_autoflush:
        return _locked_epoch(session, organization_id, store_id, scope, exclusive, expected_epoch, current)


def _locked_epoch(
    session: Session,
    organization_id: str,
    store_id: str,
    scope: tuple,
    exclusive: bool,
    expected_epoch: int | None,
    current: datetime,
) -> int:
    if session.get_bind().dialect.name == "sqlite":
        # No shared row lock exists in SQLite: the conservative write lock is the
        # fence, and it is taken before provisioning so first use serializes too.
        changed = _touch_scope_fence(session, organization_id, store_id)
        if changed.rowcount != 1:
            _provision_scope_fence(session, organization_id, store_id, current)
            changed = _touch_scope_fence(session, organization_id, store_id)
        if changed.rowcount != 1:
            raise ScopeSecurityFenceError("security fence scope disappeared under the lock")
        version = session.scalar(select(ScopeSecurityFence.version).where(*scope))
    else:
        _provision_scope_fence(session, organization_id, store_id, current)
        version = session.scalar(select(ScopeSecurityFence.version).where(*scope).with_for_update(
            read=not exclusive,
        ))
    if version is None:
        raise ScopeSecurityFenceError("security fence scope has no fence row")
    if expected_epoch is not None and version != expected_epoch:
        raise ScopeSecurityFenceError(
            f"security fence epoch moved: expected {expected_epoch}, found {version}",
        )
    return version


def lock_work_security_fence(session: Session, organization_id: str, store_id: str, *,
                             expected_epoch: int | None = None, now: datetime | None = None) -> int:
    """Same physical scope lock, distinct work epoch; grants no permission."""
    if expected_epoch is not None and (type(expected_epoch) is not int or expected_epoch <= 0):
        raise ScopeSecurityFenceError("work fence expected_epoch must be positive")
    lock_scope_security_fence(session, organization_id, store_id, now=now)
    with session.no_autoflush:
        epoch = session.scalar(select(ScopeSecurityFence.work_version).where(
            *_scope_columns(organization_id, store_id)))
    if epoch is None or (expected_epoch is not None and epoch != expected_epoch):
        raise ScopeSecurityFenceError("work security fence epoch moved")
    return epoch


def _advance_scope_fence(session, organization_id, store_id, *, invalidate_work, expected_epoch, now):
    current = _utc(now)
    epoch = lock_scope_security_fence(
        session, organization_id, store_id, exclusive=True, expected_epoch=expected_epoch, now=current,
    )
    values = dict(version=epoch + 1, updated_at=current)
    if invalidate_work:
        values['work_version'] = ScopeSecurityFence.work_version + 1
    advanced = session.execute(update(ScopeSecurityFence).where(
        ScopeSecurityFence.organization_id == organization_id,
        ScopeSecurityFence.store_id == store_id,
        ScopeSecurityFence.version == epoch,
    ).values(**values).execution_options(synchronize_session=False))
    if advanced.rowcount != 1:
        raise ScopeSecurityFenceError("security fence epoch changed under the exclusive lock")
    return epoch + 1


def advance_scope_security_fence(session: Session, organization_id: str, store_id: str, *,
                                 expected_epoch: int | None = None, now: datetime | None = None) -> int:
    """Security changes invalidate both session and work authority; never commits."""
    return _advance_scope_fence(session, organization_id, store_id, invalidate_work=True,
                                expected_epoch=expected_epoch, now=now)


def advance_session_logout_fence(session: Session, organization_id: str, store_id: str, *,
                                 now: datetime | None = None) -> int:
    """Only normal single-session logout preserves previously issued work grants.

    The original epoch still advances, so every legacy execution/session and
    human approval keeps its old revocation semantics. This is not logout-all.
    """
    return _advance_scope_fence(session, organization_id, store_id, invalidate_work=False,
                                expected_epoch=None, now=now)


def affected_authorization_scopes(
    session: Session,
    *,
    organization_id: str,
    store_id: str | None = None,
    subject_id: str | None = None,
    role_key: str | None = None,
) -> tuple[tuple[str, str], ...]:
    """Resolve which scope epochs one authorization change has to advance.

    Grants are scoped by organization/store, so a change confined to one store
    must not invalidate every queued authorization in the whole organization. No
    store means the change spans every existing store of the organization --
    inactive ones included, so a store that is re-enabled later cannot inherit a
    stale epoch -- narrowed by the subject/role selectors that actually appear in
    that domain, and ordered by organization/store for a stable lock order.

    Call this before staging the grant change itself, so the scopes are derived
    from what is committed rather than from unflushed intent.
    """
    if not organization_id:
        raise ScopeSecurityFenceError("security fence requires an organization scope")
    if store_id is not None:
        return ((organization_id, store_id),)
    store_ids = set(session.scalars(select(Store.id).where(
        Store.organization_id == organization_id,
    )))
    if subject_id is not None or role_key is not None:
        selected: set[str] = set()
        if subject_id is not None:
            selected |= set(session.scalars(select(RoleAssignment.store_id).where(
                RoleAssignment.organization_id == organization_id,
                RoleAssignment.subject_id == subject_id,
            )))
            selected |= set(session.scalars(select(PermissionGrant.store_id).where(
                PermissionGrant.organization_id == organization_id,
                PermissionGrant.subject_id == subject_id,
            )))
            selected |= set(session.scalars(select(EmployeeAccount.store_id).where(
                EmployeeAccount.organization_id == organization_id,
                EmployeeAccount.subject_id == subject_id,
            )))
        if role_key is not None:
            selected |= set(session.scalars(select(RoleAssignment.store_id).where(
                RoleAssignment.organization_id == organization_id,
                RoleAssignment.role_key == role_key,
            )))
            selected |= set(session.scalars(select(PermissionGrant.store_id).where(
                PermissionGrant.organization_id == organization_id,
                PermissionGrant.role_key == role_key,
            )))
        store_ids &= selected
    return tuple(sorted((organization_id, item) for item in store_ids))


def advance_authorization_fences(
    session: Session,
    *,
    organization_id: str,
    store_id: str | None = None,
    subject_id: str | None = None,
    role_key: str | None = None,
    expected_epochs: dict[tuple[str, str], int] | None = None,
    now: datetime | None = None,
) -> dict[tuple[str, str], int]:
    """Advance every scope a change covers, in organization/store order."""
    scopes = affected_authorization_scopes(
        session,
        organization_id=organization_id,
        store_id=store_id,
        subject_id=subject_id,
        role_key=role_key,
    )
    if expected_epochs is not None and set(expected_epochs) - set(scopes):
        raise ScopeSecurityFenceError("expected epoch given for a scope outside the change domain")
    current = _utc(now)
    advanced: dict[tuple[str, str], int] = {}
    for scope_organization_id, scope_store_id in scopes:
        expected = None if expected_epochs is None else expected_epochs.get(
            (scope_organization_id, scope_store_id),
        )
        advanced[(scope_organization_id, scope_store_id)] = advance_scope_security_fence(
            session,
            scope_organization_id,
            scope_store_id,
            expected_epoch=expected,
            now=current,
        )
    return advanced


def lock_employee_authority(session: Session, actor: AuthenticatedSubject) -> None:
    if type(actor) is not AuthenticatedSubject:
        raise HTTPException(403, 'human_session_required')
    if actor.account_id is None or actor.session_id is None:
        raise HTTPException(403, "human_session_required")
    # No autoflush inside the guard: a pending grant change must not be written
    # before the fence, and a store only created in this session must be
    # persisted by the caller first. The guard reports the missing scope instead.
    with session.no_autoflush:
        refresh_authority(session)
        _lock_employee_authority(session, actor)


def _lock_employee_authority(session: Session, actor: AuthenticatedSubject) -> None:
    # The shared fence lock comes first so a revocation-capable transaction in
    # this scope either commits before the snapshot below or waits for it.
    try:
        lock_scope_security_fence(session, actor.organization_id, actor.store_id)
    except ScopeSecurityFenceError as error:
        raise HTTPException(403, "store_unavailable") from error
    # SQLite ignores SELECT FOR UPDATE, so take its write lock first. PostgreSQL
    # also serializes local command receipts for this store via this row update.
    changed = session.execute(update(Store).where(
        Store.organization_id == actor.organization_id,
        Store.id == actor.store_id, Store.active.is_(True),
    ).values(active=Store.active).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise HTTPException(403, "store_unavailable")
    account_id = session.scalar(select(EmployeeAccount.id).where(
        EmployeeAccount.id == actor.account_id,
        EmployeeAccount.organization_id == actor.organization_id,
        EmployeeAccount.store_id == actor.store_id,
        EmployeeAccount.subject_id == actor.subject_id,
        EmployeeAccount.active.is_(True),
    ).with_for_update())
    session_id = session.scalar(select(LoginSession.id).where(
        LoginSession.id == actor.session_id,
        LoginSession.account_id == actor.account_id,
        LoginSession.revoked_at.is_(None),
    ).with_for_update())
    if account_id is None or session_id is None:
        raise HTTPException(403, "employee_scope_denied")
    refresh_authority(session)
    # Caller MUST revalidate session and action permission after acquiring these
    # locks, and keep the same transaction through receipt/audit commit.
