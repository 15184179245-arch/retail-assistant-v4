from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject, ensure_subject_active
from backend.app.modules.approvals.models import ApprovalIdempotency, ApprovalRequest
from backend.app.modules.approvals.repository import (
    compare_and_set_approval_state,
    get_approval,
    get_idempotency,
    list_page_for_scope,
)
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.identity.models import Subject


def _fail(status: int, detail: str) -> None:
    raise HTTPException(status_code=status, detail=detail)


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        _fail(400, f"approval_{field}_timezone_required")
    return value.astimezone(timezone.utc)


def _stored_utc(value: datetime) -> datetime:
    # SQLite does not preserve timezone metadata for DATETIME columns. All
    # persisted approval timestamps are normalized to UTC before insertion.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _require_person(session: Session, actor: AuthenticatedSubject) -> None:
    subject_type = session.scalar(select(Subject.subject_type).where(
        Subject.organization_id == actor.organization_id, Subject.id == actor.subject_id))
    if subject_type != "person":
        _fail(403, "approval_person_required")


def _now(value: datetime | None) -> datetime:
    return _utc(value, field="time") if value else datetime.now(timezone.utc)


def _key(value: str) -> str:
    if not value or not value.strip() or len(value) > 200:
        _fail(400, "idempotency_key_invalid")
    return value.strip()


def _digest(operation: str, values: dict) -> str:
    packed = json.dumps({"operation": operation, **values}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def _replay(session: Session, actor: AuthenticatedSubject, key: str, operation: str, digest: str) -> ApprovalRequest | None:
    existing = get_idempotency(session, actor.organization_id, actor.store_id, key)
    if existing is None:
        return None
    if existing.operation != operation or existing.payload_digest != digest or existing.actor_id != actor.subject_id:
        _fail(409, "idempotency_conflict")
    approval = get_approval(session, actor.organization_id, actor.store_id, existing.approval_id)
    if approval is None:
        _fail(409, "approval_conflict")
    return approval


def _append_operation(
    session: Session,
    *,
    actor: AuthenticatedSubject,
    key: str,
    operation: str,
    digest: str,
    approval: ApprovalRequest,
    audit_action: str,
    details: dict,
) -> None:
    record_event(
        session,
        actor_id=actor.subject_id,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        action=audit_action,
        target_type="approval_request",
        target_id=approval.id,
        idempotency_key=f"approval:{approval.id}:{audit_action}",
        details=details,
    )
    session.add(ApprovalIdempotency(
        id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        idempotency_key=key, operation=operation, payload_digest=digest,
        approval_id=approval.id, actor_id=actor.subject_id,
    ))
    session.flush()


def _ensure_real_sqlite_transaction(session: Session) -> None:
    # sqlite3's legacy transaction mode may leave SELECTs outside a driver
    # transaction. A released SAVEPOINT would then commit independently of the
    # caller; explicitly begin before our idempotency savepoint.
    connection = session.connection()
    if connection.dialect.name == "sqlite":
        raw_connection = connection.connection.driver_connection
        if not raw_connection.in_transaction:
            connection.exec_driver_sql("BEGIN")


def _run_idempotent(session: Session, *, actor: AuthenticatedSubject, key: str, operation: str, digest: str, apply):
    replayed = _replay(session, actor, key, operation, digest)
    if replayed is not None:
        return replayed
    _ensure_real_sqlite_transaction(session)
    try:
        with session.begin_nested():
            approval, audit_action, details = apply()
            _append_operation(
                session, actor=actor, key=key, operation=operation, digest=digest,
                approval=approval, audit_action=audit_action, details=details,
            )
        return approval
    except IntegrityError:
        # A concurrent replay can win the unique scoped idempotency key. The
        # savepoint rolls back only this operation, preserving the caller tx.
        replayed = _replay(session, actor, key, operation, digest)
        if replayed is not None:
            return replayed
        _fail(409, "approval_conflict")


def _scoped_request(session: Session, actor: AuthenticatedSubject, approval_id: str, *, lock: bool = False) -> ApprovalRequest:
    approval = get_approval(session, actor.organization_id, actor.store_id, approval_id, lock=lock)
    if approval is None:
        _fail(404, "approval_not_found")
    return approval


def _authorize_approval_read(session: Session, actor: AuthenticatedSubject, current: datetime) -> None:
    authorize_scope(session, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission="approvals.read", now=current)
    _require_person(session, actor)


def list_approvals(
    session: Session,
    actor: AuthenticatedSubject,
    *,
    state: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    now: datetime | None = None,
) -> tuple[list[ApprovalRequest], str | None]:
    current = _now(now)
    _authorize_approval_read(session, actor, current)
    boundary = None
    if cursor is not None:
        boundary = get_approval(session, actor.organization_id, actor.store_id, cursor)
        if boundary is None:
            _fail(400, "approval_cursor_invalid")
    return list_page_for_scope(
        session, actor.organization_id, actor.store_id,
        state=state, cursor=boundary, limit=limit,
    )


def get_approval_for_read(
    session: Session,
    actor: AuthenticatedSubject,
    *,
    approval_id: str,
    now: datetime | None = None,
) -> ApprovalRequest:
    _authorize_approval_read(session, actor, _now(now))
    return _scoped_request(session, actor, approval_id)


def request_approval(
    session: Session,
    actor: AuthenticatedSubject,
    *,
    subject_type: str,
    subject_id: str,
    action_key: str,
    reason: str,
    expires_at: datetime,
    idempotency_key: str,
    now: datetime | None = None,
) -> ApprovalRequest:
    current = _now(now)
    ensure_subject_active(actor, current)
    authorize_scope(session, actor, organization_id=actor.organization_id, store_id=actor.store_id, permission="approvals.request", now=current)
    key = _key(idempotency_key)
    deadline = _utc(expires_at, field="expiry")
    if not all((subject_type.strip(), subject_id.strip(), action_key.strip(), reason.strip())):
        _fail(400, "approval_invalid")
    values = {
        "actor_id": actor.subject_id, "subject_type": subject_type, "subject_id": subject_id,
        "action_key": action_key, "reason": reason, "expires_at": deadline.isoformat(),
    }
    digest = _digest("request", values)
    replayed = _replay(session, actor, key, "request", digest)
    if replayed is not None:
        return replayed
    if deadline <= current:
        _fail(400, "approval_expiry_invalid")

    def apply():
        approval = ApprovalRequest(
            id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
            requester_id=actor.subject_id, subject_type=subject_type, subject_id=subject_id,
            action_key=action_key, reason=reason, state="requested", requested_at=current,
            expires_at=deadline,
        )
        session.add(approval)
        session.flush()
        return approval, "approval.requested", {"state": "requested", "action_key": action_key}

    return _run_idempotent(session, actor=actor, key=key, operation="request", digest=digest, apply=apply)


def decide_approval(
    session: Session,
    actor: AuthenticatedSubject,
    *,
    approval_id: str,
    decision: str,
    reason: str,
    idempotency_key: str,
    now: datetime | None = None,
) -> ApprovalRequest:
    current = _now(now)
    ensure_subject_active(actor, current)
    authorize_scope(session, actor, organization_id=actor.organization_id, store_id=actor.store_id, permission="approvals.decide", now=current)
    _require_person(session, actor)
    if decision not in ("approved", "rejected") or not reason.strip():
        _fail(400, "approval_invalid")
    key = _key(idempotency_key)
    digest = _digest("decision", {"actor_id": actor.subject_id, "approval_id": approval_id, "decision": decision, "reason": reason})
    replayed = _replay(session, actor, key, "decision", digest)
    if replayed is not None:
        return replayed
    approval = _scoped_request(session, actor, approval_id, lock=True)
    if approval.requester_id == actor.subject_id:
        _fail(403, "approval_self_decision")
    if approval.state != "requested":
        _fail(409, "approval_conflict")

    def apply():
        if _stored_utc(approval.expires_at) <= current:
            values = {"state": "expired", "expired_by": actor.subject_id, "expired_at": current}
            action, details = "approval.expired", {"from_state": "requested", "to_state": "expired"}
        else:
            values = {"state": decision, "decided_by": actor.subject_id, "decided_at": current, "decision_reason": reason}
            action, details = f"approval.{decision}", {"from_state": "requested", "to_state": decision}
        if not compare_and_set_approval_state(session, organization_id=actor.organization_id, store_id=actor.store_id,
                approval_id=approval.id, expected_state="requested", values=values):
            _fail(409, "approval_conflict")
        session.refresh(approval)
        if approval.state == "expired":
            return approval, "approval.expired", {"from_state": "requested", "to_state": "expired"}
        return approval, action, details

    return _run_idempotent(session, actor=actor, key=key, operation="decision", digest=digest, apply=apply)


def cancel_approval(
    session: Session,
    actor: AuthenticatedSubject,
    *,
    approval_id: str,
    reason: str,
    idempotency_key: str,
    now: datetime | None = None,
) -> ApprovalRequest:
    current = _now(now)
    ensure_subject_active(actor, current)
    authorize_scope(session, actor, organization_id=actor.organization_id, store_id=actor.store_id, permission="approvals.cancel", now=current)
    key = _key(idempotency_key)
    if not reason.strip():
        _fail(400, "approval_invalid")
    digest = _digest("cancel", {"actor_id": actor.subject_id, "approval_id": approval_id, "reason": reason})
    replayed = _replay(session, actor, key, "cancel", digest)
    if replayed is not None:
        return replayed
    approval = _scoped_request(session, actor, approval_id, lock=True)
    if approval.requester_id != actor.subject_id:
        _fail(403, "approval_cancel_forbidden")
    if approval.state != "requested":
        _fail(409, "approval_conflict")

    def apply():
        if _stored_utc(approval.expires_at) <= current:
            values = {"state": "expired", "expired_by": actor.subject_id, "expired_at": current}
            action, details = "approval.expired", {"from_state": "requested", "to_state": "expired"}
        else:
            values = {"state": "cancelled", "cancelled_by": actor.subject_id, "cancelled_at": current, "cancellation_reason": reason}
            action, details = "approval.cancelled", {"from_state": "requested", "to_state": "cancelled"}
        if not compare_and_set_approval_state(session, organization_id=actor.organization_id, store_id=actor.store_id,
                approval_id=approval.id, expected_state="requested", values=values):
            _fail(409, "approval_conflict")
        session.refresh(approval)
        return approval, action, details

    return _run_idempotent(session, actor=actor, key=key, operation="cancel", digest=digest, apply=apply)


def expire_approval(
    session: Session,
    actor: AuthenticatedSubject,
    *,
    approval_id: str,
    idempotency_key: str,
    now: datetime | None = None,
) -> ApprovalRequest:
    current = _now(now)
    ensure_subject_active(actor, current)
    authorize_scope(session, actor, organization_id=actor.organization_id, store_id=actor.store_id, permission="approvals.expire", now=current)
    key = _key(idempotency_key)
    digest = _digest("expire", {"actor_id": actor.subject_id, "approval_id": approval_id})
    replayed = _replay(session, actor, key, "expire", digest)
    if replayed is not None:
        return replayed
    approval = _scoped_request(session, actor, approval_id, lock=True)
    if approval.state != "requested":
        _fail(409, "approval_conflict")
    if _stored_utc(approval.expires_at) > current:
        _fail(409, "approval_not_expired")

    def apply():
        if not compare_and_set_approval_state(session, organization_id=actor.organization_id, store_id=actor.store_id,
                approval_id=approval.id, expected_state="requested",
                values={"state": "expired", "expired_by": actor.subject_id, "expired_at": current}):
            _fail(409, "approval_conflict")
        session.refresh(approval)
        return approval, "approval.expired", {"from_state": "requested", "to_state": "expired"}

    return _run_idempotent(session, actor=actor, key=key, operation="expire", digest=digest, apply=apply)
