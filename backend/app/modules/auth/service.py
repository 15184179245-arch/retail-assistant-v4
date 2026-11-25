"""Employee credentials and revocable sessions in caller-owned transactions."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import case, or_, select, update
from sqlalchemy.orm import Session

from backend.app.core.passwords import dummy_verify, hash_password, normalize_username, verify_password
from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.auth.models import EmployeeAccount, LoginSession
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.command_guard import advance_scope_security_fence, advance_session_logout_fence, lock_scope_security_fence
from backend.app.modules.identity.models import Organization, Store, Subject

SESSION_TTL = timedelta(hours=8)
LOCK_DURATION = timedelta(minutes=15)
MAX_FAILURES = 5


@dataclass(frozen=True)
class LoginResult:
    access_token: str
    account: EmployeeAccount
    session: LoginSession


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _now(value: datetime | None) -> datetime:
    return _utc(value) if value is not None else datetime.now(timezone.utc)


def _deny(detail: str = "invalid_credentials") -> None:
    raise HTTPException(401, detail, headers={"WWW-Authenticate": "Bearer"})


def _scope_active(session: Session, account: EmployeeAccount) -> bool:
    organization = session.get(Organization, account.organization_id)
    store = session.get(Store, account.store_id)
    person = session.get(Subject, account.subject_id)
    return bool(
        account.active and organization and organization.active and store and store.active
        and person and person.active and person.subject_type == "person"
        and store.organization_id == account.organization_id
        and person.organization_id == account.organization_id
    )


def has_usable_account(session: Session, *, organization_id: str, store_id: str,
                       subject_id: str, now: datetime | None = None) -> bool:
    """Check assignability without creating a login session or a synthetic actor."""
    current = _now(now)
    account = session.scalar(select(EmployeeAccount).where(
        EmployeeAccount.organization_id == organization_id,
        EmployeeAccount.store_id == store_id,
        EmployeeAccount.subject_id == subject_id,
    ))
    return bool(account is not None and _scope_active(session, account)
                and (account.locked_until is None or _utc(account.locked_until) <= current))


def _audit(session: Session, account: EmployeeAccount, action: str, current: datetime,
           *, actor_id: str | None = None, target_id: str | None = None) -> None:
    record_event(session, actor_id=actor_id or account.subject_id,
        organization_id=account.organization_id, store_id=account.store_id,
        action=action, target_type="employee_account", target_id=target_id or account.id,
        idempotency_key=f"auth:{uuid4()}", details={}, occurred_at=current)


def create_account(session: Session, *, username: str, password: str, organization_id: str,
                   store_id: str, subject_id: str, now: datetime | None = None) -> EmployeeAccount:
    """Trusted host provisioning primitive; its caller audits the host operator."""
    account = EmployeeAccount(id=str(uuid4()), organization_id=organization_id,
        store_id=store_id, subject_id=subject_id, username=normalize_username(username),
        password_hash=hash_password(password), active=True, version=1, failed_attempts=0,
        created_at=_now(now))
    if not _scope_active(session, account):
        raise ValueError("account requires an active person in the selected organization/store")
    session.add(account)
    session.flush()
    return account


def _failure(session: Session, account: EmployeeAccount, current: datetime) -> None:
    # Every failed attempt for a known account takes the scope fence first, even
    # when this attempt only bumps a counter. Deciding the lockout from the
    # pre-lock snapshot lets a stale count reach a lockout write through an
    # Account -> fence order (a PostgreSQL deadlock against the guard's
    # fence -> Account order) and moves the epoch outside the guarded fence.
    # Only the attempt that really locks the account advances the epoch below.
    organization_id, store_id, account_id = account.organization_id, account.store_id, account.id
    lock_scope_security_fence(session, organization_id, store_id, exclusive=True, now=current)
    session.expire_all()
    account = session.get(EmployeeAccount, account_id, populate_existing=True)
    if account is None:
        return
    # SQL expressions avoid losing parallel failed attempts through read/modify/write.
    reset = EmployeeAccount.locked_until <= current
    next_count = case((reset, 1), else_=EmployeeAccount.failed_attempts + 1)
    changed = session.execute(update(EmployeeAccount).where(
        EmployeeAccount.id == account.id, EmployeeAccount.version == account.version,
        or_(EmployeeAccount.locked_until.is_(None), EmployeeAccount.locked_until <= current),
    ).values(failed_attempts=next_count,
        locked_until=case((next_count >= MAX_FAILURES, current + LOCK_DURATION), else_=None))
      .execution_options(synchronize_session=False))
    session.refresh(account)
    if changed.rowcount == 1 and account.locked_until is not None and _utc(account.locked_until) > current:
        advance_scope_security_fence(session, account.organization_id, account.store_id, now=current)
    _audit(session, account, "auth.login_failed", current)


def _locked_account(session: Session, account: EmployeeAccount, current: datetime) -> EmployeeAccount:
    """Take the scope fence, then return the post-lock account row."""
    lock_scope_security_fence(session, account.organization_id, account.store_id,
                              exclusive=True, now=current)
    session.expire_all()
    fresh = session.get(EmployeeAccount, account.id, populate_existing=True)
    if fresh is None:
        _deny()
    return fresh


def login(session: Session, *, username: str, password: str,
          now: datetime | None = None) -> LoginResult | None:
    """Return None on denial so the route commits failed-attempt accounting."""
    current = _now(now)
    try:
        canonical = normalize_username(username)
    except ValueError:
        dummy_verify(password)
        return None
    account = session.scalar(select(EmployeeAccount).where(EmployeeAccount.username == canonical))
    if account is None:
        dummy_verify(password)
        return None
    correct = verify_password(password, account.password_hash)
    locked = account.locked_until is not None and _utc(account.locked_until) > current
    if not correct or locked or not _scope_active(session, account):
        if not locked:
            _failure(session, account, current)
        return None
    accepted = session.execute(update(EmployeeAccount).where(
        EmployeeAccount.id == account.id, EmployeeAccount.active.is_(True),
        EmployeeAccount.version == account.version, EmployeeAccount.password_hash == account.password_hash,
        or_(EmployeeAccount.locked_until.is_(None), EmployeeAccount.locked_until <= current),
    ).values(failed_attempts=0, locked_until=None).execution_options(synchronize_session=False))
    if accepted.rowcount != 1:
        return None
    token = f"sx1_{secrets.token_urlsafe(32)}"
    credential_session = LoginSession(id=str(uuid4()), account_id=account.id,
        token_hash=hashlib.sha256(token.encode("ascii")).hexdigest(), account_version=account.version,
        created_at=current, expires_at=current + SESSION_TTL)
    session.add(credential_session)
    _audit(session, account, "auth.login", current)
    session.flush()
    return LoginResult(token, account, credential_session)


def _live_pair(session: Session, credential_session: LoginSession | None, current: datetime):
    if credential_session is None or credential_session.revoked_at is not None:
        _deny()
    account = session.get(EmployeeAccount, credential_session.account_id)
    if account is None or credential_session.account_version != account.version or not _scope_active(session, account):
        _deny()
    if _utc(credential_session.expires_at) <= current:
        _deny("credential_expired")
    return account, credential_session


def authenticate_token(session: Session, token: str, *, now: datetime | None = None) -> AuthenticatedSubject:
    if re.fullmatch(r"sx1_[A-Za-z0-9_-]{43}", token) is None:
        _deny()
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    row = session.scalar(select(LoginSession).where(LoginSession.token_hash == digest))
    account, row = _live_pair(session, row, _now(now))
    return AuthenticatedSubject(account.subject_id, account.organization_id, account.store_id,
        _utc(row.expires_at), account_id=account.id, session_id=row.id, account_version=account.version)


def validate_actor_session(session: Session, actor: AuthenticatedSubject, *, now: datetime | None = None) -> EmployeeAccount:
    """Recheck revocation at the business service's authorization boundary."""
    if type(actor) is not AuthenticatedSubject:
        _deny()
    if actor.account_id is None or actor.session_id is None or actor.account_version is None:
        _deny()
    row = session.get(LoginSession, actor.session_id)
    account, row = _live_pair(session, row, _now(now))
    if (account.id, account.version, account.subject_id, account.organization_id, account.store_id) != (
        actor.account_id, actor.account_version, actor.subject_id, actor.organization_id, actor.store_id,
    ):
        _deny()
    return account


def logout(session: Session, token: str, now: datetime | None = None) -> None:
    current = _now(now)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    row = session.scalar(select(LoginSession).where(LoginSession.token_hash == digest))
    if row is None or row.revoked_at is not None:
        return  # unknown or already revoked: no authorization change, no epoch move
    account = session.get(EmployeeAccount, row.account_id)
    if account is None:
        session.execute(update(LoginSession).where(LoginSession.id == row.id,
            LoginSession.revoked_at.is_(None)).values(revoked_at=current))
        return
    # Revoking a session is a scope authorization change: exclusive fence first,
    # drop the pre-lock snapshots, only then re-read and revoke.
    lock_scope_security_fence(session, account.organization_id, account.store_id,
                              exclusive=True, now=current)
    session.expire_all()
    row = session.get(LoginSession, row.id, populate_existing=True)
    account = session.get(EmployeeAccount, account.id, populate_existing=True)
    if row is None or account is None or row.revoked_at is not None:
        return
    changed = session.execute(update(LoginSession).where(LoginSession.id == row.id,
        LoginSession.revoked_at.is_(None)).values(revoked_at=current))
    if changed.rowcount != 1:
        return
    advance_session_logout_fence(session, account.organization_id, account.store_id, now=current)
    _audit(session, account, "auth.logout", current)


def _invalidate(session: Session, account: EmployeeAccount, current: datetime, *, password_hash: str | None = None,
                deactivate: bool = False) -> None:
    values = {"version": EmployeeAccount.version + 1}
    if password_hash is not None:
        values.update(password_hash=password_hash, failed_attempts=0, locked_until=None)
    if deactivate:
        values["active"] = False
    changed = session.execute(update(EmployeeAccount).where(EmployeeAccount.id == account.id,
        EmployeeAccount.version == account.version).values(**values)
        .execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        _deny()
    session.execute(update(LoginSession).where(LoginSession.account_id == account.id,
        LoginSession.revoked_at.is_(None)).values(revoked_at=current))
    session.refresh(account)


def logout_all(session: Session, actor: AuthenticatedSubject, now: datetime | None = None) -> None:
    current = _now(now)
    account = validate_actor_session(session, actor, now=current)
    account = _locked_account(session, account, current)
    validate_actor_session(session, actor, now=current)
    _invalidate(session, account, current)
    advance_scope_security_fence(session, account.organization_id, account.store_id, now=current)
    _audit(session, account, "auth.logout_all", current)


def change_password(session: Session, actor: AuthenticatedSubject, *, current_password: str,
                    new_password: str, now: datetime | None = None) -> bool:
    current = _now(now)
    account = validate_actor_session(session, actor, now=current)
    correct = verify_password(current_password, account.password_hash)
    locked = account.locked_until is not None and _utc(account.locked_until) > current
    if not correct or locked:
        if not locked:
            _failure(session, account, current)
        return False
    account = _locked_account(session, account, current)
    # The actor revalidation comes first: a request whose authorization snapshot
    # is stale must fail shut (401) instead of silently applying to newer rows.
    validate_actor_session(session, actor, now=current)
    if not verify_password(current_password, account.password_hash):
        _deny()
    if account.locked_until is not None and _utc(account.locked_until) > current:
        return False
    _invalidate(session, account, current, password_hash=hash_password(new_password))
    advance_scope_security_fence(session, account.organization_id, account.store_id, now=current)
    _audit(session, account, "auth.password_changed", current)
    return True


def reset_password(session: Session, account: EmployeeAccount, *, new_password: str,
                   actor_id: str, now: datetime | None = None) -> None:
    current = _now(now)
    account = _locked_account(session, account, current)
    _invalidate(session, account, current, password_hash=hash_password(new_password))
    advance_scope_security_fence(session, account.organization_id, account.store_id, now=current)
    _audit(session, account, "auth.password_reset", current, actor_id=actor_id)


def disable_account(session: Session, account: EmployeeAccount, *, actor_id: str,
                    now: datetime | None = None) -> None:
    current = _now(now)
    account = _locked_account(session, account, current)
    _invalidate(session, account, current, deactivate=True)
    advance_scope_security_fence(session, account.organization_id, account.store_id, now=current)
    _audit(session, account, "auth.account_disabled", current, actor_id=actor_id)
