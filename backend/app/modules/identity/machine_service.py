"""Distinct, revocable machine credentials. No employee/API session is synthesized."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from backend.app.core.security import AuthenticatedSubject
from backend.app.db.base import Base
from backend.app.modules.audit.repository import get_by_idempotency_key
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.command_guard import advance_scope_security_fence, lock_scope_security_fence
from backend.app.modules.identity.models import Organization, Store, Subject
from backend.app.modules.identity.service import authorize_scope

# These are machine-only, internal action identifiers, not person permissions.
MACHINE_ACTIONS = {
    "worker": frozenset({"worker.agent.execute", "worker.agent.recover",
                         "worker.outbox.claim", "worker.outbox.ack",
                         "worker.office.schedule.materialize"}),
    "pos_plugin": frozenset({"pos.dispatch.receive"}),
}
MAX_TTL = timedelta(days=90)
MACHINE_TOKEN_RE = re.compile(r"sx2_[A-Za-z0-9_-]{43}\Z")


class MachineIdentity(Base):
    __tablename__ = "machine_identities"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(36), nullable=False)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_machine_id_scope"),
        UniqueConstraint("organization_id", "store_id", "label", name="uq_machine_label_scope"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_machine_store", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "subject_id"], ["subjects.organization_id", "subjects.id"], name="fk_machine_subject", ondelete="RESTRICT"),
        CheckConstraint("purpose IN ('worker','pos_plugin')", name="ck_machine_purpose"),
        CheckConstraint("version >= 1", name="ck_machine_version"),
    )


class MachineCredential(Base):
    __tablename__ = "machine_credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    machine_id: Mapped[str] = mapped_column(String(36), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    issued_by: Mapped[str] = mapped_column(String(36), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id", "machine_id"], ["machine_identities.organization_id", "machine_identities.store_id", "machine_identities.id"], name="fk_machine_credential_identity", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "issued_by"], ["subjects.organization_id", "subjects.id"], name="fk_machine_credential_issuer", ondelete="RESTRICT"),
        UniqueConstraint("token_hash", name="uq_machine_credentials_token_hash"),
        UniqueConstraint("machine_id", "version", name="uq_machine_credential_version"),
        Index("ix_machine_credential_scope", "organization_id", "store_id", "machine_id", "revoked_at"),
        CheckConstraint("version >= 1", name="ck_machine_credential_version"),
        CheckConstraint("expires_at > issued_at", name="ck_machine_credential_dates"),
    )


@dataclass(frozen=True)
class MachinePrincipal:
    machine_id: str
    credential_id: str
    subject_id: str
    organization_id: str
    store_id: str
    purpose: str
    version: int
    auth_expires_at: datetime


def _utc(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("machine credential time must have a timezone")
    return value.astimezone(timezone.utc)


def _stored_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _deny(detail: str = "invalid_credentials") -> None:
    raise HTTPException(401, detail, headers={"WWW-Authenticate": "Bearer"})


def _validate_operator(session: Session, operator: AuthenticatedSubject, org: str, store: str, now: datetime) -> None:
    # authorize_scope also rechecks the exact employee login session at the service boundary.
    from backend.app.modules.auth.service import validate_actor_session
    validate_actor_session(session, operator, now=now)
    authorize_scope(session, operator, organization_id=org, store_id=store, permission="machine.manage", now=now)
    subject = session.get(Subject, operator.subject_id)
    if subject is None or subject.subject_type != "person":
        raise HTTPException(403, "machine_manage_person_required")


def _request_key(request_id: str) -> str:
    return "machine:" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()


def _check_request(reason: str, request_id: str) -> tuple[str, str]:
    reason, request_id = reason.strip(), request_id.strip()
    if not 1 <= len(reason) <= 500 or not 1 <= len(request_id) <= 200:
        raise ValueError("machine reason/request-id invalid")
    return reason, request_id


def _expires(value: datetime, now: datetime) -> datetime:
    end = _utc(value)
    if end <= now or end - now > MAX_TTL:
        raise ValueError("machine expiry must be in the next 90 days")
    return end


def _replay(session: Session, org: str, store: str, key: str, actor: str, action: str, request: dict) -> dict | None:
    previous = get_by_idempotency_key(session, org, store, key)
    if previous is None:
        return None
    if (previous.actor_id != actor or previous.action != action
            or previous.target_type != "machine_identity" or previous.details.get("request") != request
            or not isinstance(previous.details.get("result"), dict)):
        raise ValueError("machine request-id conflict")
    return {**previous.details["result"], "replayed": True, "access_token": None}


def _receipt(session: Session, operator: AuthenticatedSubject, key: str, action: str,
             machine: MachineIdentity, request: dict, result: dict, now: datetime) -> None:
    record_event(session, actor_id=operator.subject_id,
        organization_id=machine.organization_id, store_id=machine.store_id,
        action=action, target_type="machine_identity", target_id=machine.id,
        idempotency_key=key, occurred_at=now, details={"request": request, "result": result})


def _mint(session: Session, machine: MachineIdentity, operator_id: str, expires: datetime, now: datetime) -> tuple[MachineCredential, str]:
    token = "sx2_" + secrets.token_urlsafe(32)
    credential = MachineCredential(id=str(uuid4()), organization_id=machine.organization_id,
        store_id=machine.store_id, machine_id=machine.id, token_hash=hashlib.sha256(token.encode("ascii")).hexdigest(),
        version=machine.version, issued_by=operator_id, issued_at=now, expires_at=expires)
    session.add(credential)
    session.flush()
    return credential, token


def issue_machine(session: Session, operator: AuthenticatedSubject, *, organization_id: str,
                  store_id: str, purpose: str, label: str, expires_at: datetime,
                  reason: str, request_id: str, now: datetime | None = None) -> dict:
    current = _utc(now)
    reason, request_id = _check_request(reason, request_id)
    label = label.strip()
    if purpose not in MACHINE_ACTIONS or not 1 <= len(label) <= 80:
        raise ValueError("invalid machine purpose/label")
    expiry = _expires(expires_at, current)
    _validate_operator(session, operator, organization_id, store_id, current)
    # Exclusive scope fence first; it serializes with every other revocation in
    # this organization/store and is held to the caller's commit.
    lock_scope_security_fence(session, organization_id, store_id, exclusive=True, now=current)
    # One store row lock serializes concurrent issue requests and audit-idempotency checks.
    locked = session.execute(update(Store).where(Store.id == store_id,
        Store.organization_id == organization_id, Store.active.is_(True))
        .values(active=Store.active).execution_options(synchronize_session=False))
    if locked.rowcount != 1:
        raise ValueError("inactive machine store")
    session.expire_all()  # drop snapshots taken before the fence
    _validate_operator(session, operator, organization_id, store_id, current)
    key, request = _request_key(request_id), {"purpose": purpose, "label": label,
        "expires_at": expiry.isoformat(), "reason": reason}
    existing = _replay(session, organization_id, store_id, key, operator.subject_id, "machine.issue", request)
    if existing is not None:
        return existing
    if session.scalar(select(MachineIdentity.id).where(MachineIdentity.organization_id == organization_id,
            MachineIdentity.store_id == store_id, MachineIdentity.label == label)) is not None:
        raise ValueError("machine label already exists")
    subject = Subject(id=str(uuid4()), organization_id=organization_id,
        display_name=label, subject_type="service", active=True)
    session.add(subject)
    session.flush()
    machine = MachineIdentity(id=str(uuid4()), organization_id=organization_id, store_id=store_id,
        subject_id=subject.id, label=label, purpose=purpose, version=1, active=True, created_at=current)
    session.add(machine)
    session.flush()
    credential, token = _mint(session, machine, operator.subject_id, expiry, current)
    result = {"machine_id": machine.id, "credential_id": credential.id,
        "purpose": purpose, "version": 1, "expires_at": expiry.isoformat()}
    advance_scope_security_fence(session, organization_id, store_id, now=current)
    _receipt(session, operator, key, "machine.issue", machine, request, result, current)
    session.flush()
    return {**result, "replayed": False, "access_token": token}


def _machine_for_operator(session: Session, operator: AuthenticatedSubject, machine_id: str, now: datetime) -> MachineIdentity:
    _validate_operator(session, operator, operator.organization_id, operator.store_id, now)
    machine = session.scalar(select(MachineIdentity).where(MachineIdentity.id == machine_id,
        MachineIdentity.organization_id == operator.organization_id, MachineIdentity.store_id == operator.store_id))
    if machine is None:
        raise HTTPException(403, "store_scope_denied")
    return machine


def rotate_machine(session: Session, operator: AuthenticatedSubject, *, machine_id: str,
                   expires_at: datetime, reason: str, request_id: str, now: datetime | None = None) -> dict:
    current = _utc(now)
    expiry = _expires(expires_at, current)
    reason, request_id = _check_request(reason, request_id)
    machine = _machine_for_operator(session, operator, machine_id, current)
    lock_scope_security_fence(session, machine.organization_id, machine.store_id,
                              exclusive=True, now=current)
    session.expire_all()  # drop snapshots taken before the fence
    machine = _fresh(session, MachineIdentity, machine.id)
    if machine is None:
        raise HTTPException(403, "store_scope_denied")
    _validate_operator(session, operator, machine.organization_id, machine.store_id, current)
    key = _request_key(request_id)
    request = {"machine_id": machine_id, "expires_at": expiry.isoformat(), "reason": reason}
    replay = _replay(session, machine.organization_id, machine.store_id, key, operator.subject_id, "machine.rotate", request)
    if replay is not None:
        return replay
    if not machine.active:
        raise ValueError("inactive machine")
    changed = session.execute(update(MachineIdentity).where(MachineIdentity.id == machine.id,
        MachineIdentity.version == machine.version, MachineIdentity.active.is_(True))
        .values(version=MachineIdentity.version + 1).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise ValueError("machine version conflict")
    session.refresh(machine)
    session.execute(update(MachineCredential).where(MachineCredential.machine_id == machine.id,
        MachineCredential.revoked_at.is_(None)).values(revoked_at=current)
        .execution_options(synchronize_session=False))
    session.expire_all()  # invalidate cached credential rows after set-based revocation
    _validate_operator(session, operator, machine.organization_id, machine.store_id, current)
    credential, token = _mint(session, machine, operator.subject_id, expiry, current)
    result = {"machine_id": machine.id, "credential_id": credential.id,
        "purpose": machine.purpose, "version": machine.version, "expires_at": expiry.isoformat()}
    advance_scope_security_fence(session, machine.organization_id, machine.store_id, now=current)
    _receipt(session, operator, key, "machine.rotate", machine, request, result, current)
    session.flush()
    return {**result, "replayed": False, "access_token": token}


def revoke_machine(session: Session, operator: AuthenticatedSubject, *, machine_id: str,
                   reason: str, request_id: str, now: datetime | None = None) -> dict:
    current = _utc(now)
    reason, request_id = _check_request(reason, request_id)
    machine = _machine_for_operator(session, operator, machine_id, current)
    lock_scope_security_fence(session, machine.organization_id, machine.store_id,
                              exclusive=True, now=current)
    session.expire_all()  # drop snapshots taken before the fence
    machine = _fresh(session, MachineIdentity, machine.id)
    if machine is None:
        raise HTTPException(403, "store_scope_denied")
    _validate_operator(session, operator, machine.organization_id, machine.store_id, current)
    key = _request_key(request_id)
    request = {"machine_id": machine_id, "reason": reason}
    replay = _replay(session, machine.organization_id, machine.store_id, key, operator.subject_id, "machine.revoke", request)
    if replay is not None:
        return replay
    if not machine.active:
        raise ValueError("inactive machine")
    changed = session.execute(update(MachineIdentity).where(MachineIdentity.id == machine.id,
        MachineIdentity.version == machine.version, MachineIdentity.active.is_(True))
        .values(version=MachineIdentity.version + 1, active=False)
        .execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise ValueError("machine version conflict")
    session.execute(update(MachineCredential).where(MachineCredential.machine_id == machine.id,
        MachineCredential.revoked_at.is_(None)).values(revoked_at=current)
        .execution_options(synchronize_session=False))
    # Each machine owns an isolated service subject; revoke it as well as its tokens.
    session.execute(update(Subject).where(Subject.id == machine.subject_id,
        Subject.organization_id == machine.organization_id, Subject.subject_type == "service")
        .values(active=False).execution_options(synchronize_session=False))
    session.expire_all()  # fail closed for a principal cached in this same transaction
    _validate_operator(session, operator, machine.organization_id, machine.store_id, current)
    result = {"machine_id": machine.id, "revoked": True}
    advance_scope_security_fence(session, machine.organization_id, machine.store_id, now=current)
    _receipt(session, operator, key, "machine.revoke", machine, request, result, current)
    session.flush()
    return {**result, "replayed": False, "access_token": None}


def _fresh(session: Session, model, object_id: str):
    """Bypass SQLAlchemy identity-map snapshots at machine authorization boundaries."""
    return session.scalar(select(model).where(model.id == object_id).execution_options(populate_existing=True))


def authenticate_machine_token(session: Session, token: str, *, now: datetime | None = None) -> MachinePrincipal:
    if MACHINE_TOKEN_RE.fullmatch(token) is None:
        _deny()
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    credential = session.scalar(select(MachineCredential).where(MachineCredential.token_hash == digest).execution_options(populate_existing=True))
    if credential is None:
        _deny()
    machine = _fresh(session, MachineIdentity, credential.machine_id)
    subject = _fresh(session, Subject, machine.subject_id) if machine is not None else None
    organization = _fresh(session, Organization, credential.organization_id)
    store = _fresh(session, Store, credential.store_id)
    if (machine is None or not machine.active or credential.revoked_at is not None
            or credential.version != machine.version or machine.organization_id != credential.organization_id
            or machine.store_id != credential.store_id or machine.purpose not in MACHINE_ACTIONS
            or subject is None or subject.subject_type != "service" or not subject.active
            or subject.organization_id != machine.organization_id or not organization or not organization.active
            or not store or not store.active or store.organization_id != machine.organization_id):
        _deny()
    expiry = _stored_utc(credential.expires_at)
    if expiry <= _utc(now):
        _deny("credential_expired")
    return MachinePrincipal(machine.id, credential.id, machine.subject_id, machine.organization_id,
                            machine.store_id, machine.purpose, machine.version, expiry)


def authorize_machine_action(session: Session, principal: MachinePrincipal, *,
                             organization_id: str, store_id: str, action: str,
                             now: datetime | None = None) -> None:
    """Must be called at each internal command boundary, not only at token parsing."""
    if (organization_id, store_id) != (principal.organization_id, principal.store_id):
        raise HTTPException(403, "store_scope_denied")
    if action not in MACHINE_ACTIONS.get(principal.purpose, frozenset()):
        raise HTTPException(403, "machine_action_denied")
    credential = _fresh(session, MachineCredential, principal.credential_id)
    machine = _fresh(session, MachineIdentity, principal.machine_id)
    subject = _fresh(session, Subject, principal.subject_id)
    organization = _fresh(session, Organization, principal.organization_id)
    store = _fresh(session, Store, principal.store_id)
    if (credential is None or machine is None or subject is None or not subject.active
            or subject.subject_type != "service" or organization is None or not organization.active
            or store is None or not store.active or store.organization_id != principal.organization_id
            or not machine.active or credential.revoked_at is not None
            or credential.machine_id != principal.machine_id or credential.version != principal.version
            or machine.version != principal.version or machine.purpose != principal.purpose
            or machine.subject_id != principal.subject_id or machine.organization_id != principal.organization_id
            or machine.store_id != principal.store_id or credential.organization_id != principal.organization_id
            or credential.store_id != principal.store_id or subject.organization_id != principal.organization_id
            or _stored_utc(credential.expires_at) <= _utc(now)):
        _deny()
