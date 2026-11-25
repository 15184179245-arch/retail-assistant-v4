"""Deterministic in-app task alerts. No outbound delivery or arbitrary recipients.

Task transition calls must run inside the existing task transaction.
Overdue reconciliation requires a separately granted human operator.
Escalation is deliberately fail-closed until a real approved policy exists.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.audit.service import record_event
from backend.app.modules.audit.repository import get_by_idempotency_key
from backend.app.modules.identity.models import Subject
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.notifications.models import Notification, NotificationCommand
from backend.app.modules.tasks.models import Task

TASK_RULE = "task-owner-v1"
OVERDUE_RULE = "task-due-v1"


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise HTTPException(400, "notification_time_timezone_required")
    return current.astimezone(timezone.utc)


def _authorize(session: Session, actor: AuthenticatedSubject, permission: str) -> None:
    if actor.account_id is None or actor.session_id is None:
        raise HTTPException(401, "notification_employee_login_required")
    authorize_scope(session, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission=permission)
    if session.scalar(select(Subject.subject_type).where(
            Subject.organization_id == actor.organization_id,
            Subject.id == actor.subject_id)) != "person":
        raise HTTPException(403, "notification_person_required")


def _visibility(actor: AuthenticatedSubject, current: datetime):
    """Task ownership is checked again on EVERY read, including receipt replay."""
    return (
        Notification.organization_id == actor.organization_id,
        Notification.store_id == actor.store_id,
        Notification.recipient_id == actor.subject_id,
        Task.organization_id == actor.organization_id,
        Task.store_id == actor.store_id,
        Task.id == Notification.task_id,
        or_(
            and_(Notification.kind == "review_requested",
                 Task.reviewer_id == actor.subject_id,
                 Task.decision_state == "pending"),
            and_(Notification.kind == "assigned",
                 Task.assignee_id == actor.subject_id,
                 Task.state.in_(("assigned", "in_progress"))),
            and_(Notification.kind == "overdue",
                 Task.assignee_id == actor.subject_id,
                 Task.state.in_(("assigned", "in_progress")),
                 Task.due_at <= current),
            and_(Notification.kind.in_(("manual_check", "failure")),
                 Task.created_by == actor.subject_id),
        ),
    )


def _visible_one(session: Session, actor: AuthenticatedSubject, notification_id: str,
                 current: datetime) -> Notification:
    row = session.scalar(select(Notification).join(Task, and_(
        Task.organization_id == Notification.organization_id,
        Task.store_id == Notification.store_id,
        Task.id == Notification.task_id,
    )).where(*_visibility(actor, current), Notification.id == notification_id))
    if row is None:
        raise HTTPException(404, "notification_not_found")
    return row


def list_inbox(session: Session, actor: AuthenticatedSubject, *, limit: int = 50,
               now: datetime | None = None) -> list[Notification]:
    _authorize(session, actor, "notifications.read")
    current = _now(now)
    if not 1 <= limit <= 100:
        raise HTTPException(422, "notification_limit_invalid")
    return list(session.scalars(select(Notification).join(Task, and_(
        Task.organization_id == Notification.organization_id,
        Task.store_id == Notification.store_id,
        Task.id == Notification.task_id,
    )).where(*_visibility(actor, current)).order_by(
        Notification.created_at.desc(), Notification.id.desc()).limit(limit)))


def _append(session: Session, *, task: Task, recipient_id: str | None, kind: str,
            event_key: str, rule_version: str, current: datetime) -> bool:
    """No recipient parameter comes from a model or HTTP body."""
    if recipient_id is None:
        return False
    if not 1 <= len(event_key) <= 120:
        raise ValueError("notification_event_key_invalid")
    prior = session.scalar(select(Notification.id).where(
        Notification.organization_id == task.organization_id,
        Notification.store_id == task.store_id,
        Notification.recipient_id == recipient_id,
        Notification.source_event_key == event_key,
        Notification.kind == kind))
    if prior is not None:
        return False
    session.add(Notification(
        id=str(uuid4()), organization_id=task.organization_id, store_id=task.store_id,
        recipient_id=recipient_id, task_id=task.id, source_event_key=event_key,
        kind=kind, rule_version=rule_version, version=1, created_at=current,
    ))
    # get_session uses autoflush=False: persist before a second event-key lookup.
    # The caller still owns the surrounding transaction/savepoint.
    session.flush()
    return True


def notify_task_transition(session: Session, *, task: Task, event_id: str,
                           now: datetime) -> None:
    """Called only from the already authorized, idempotent task transaction."""
    current = _now(now)
    kind, recipient = None, None
    if task.decision_state == "rejected":
        kind, recipient = "failure", task.created_by
    elif task.decision_state in ("deferred", "info_requested"):
        kind, recipient = "manual_check", task.created_by
    elif task.state == "suggested" and task.reviewer_id and task.decision_state == "pending":
        kind, recipient = "review_requested", task.reviewer_id
    elif task.state == "assigned" and task.assignee_id:
        kind, recipient = "assigned", task.assignee_id
    if kind is not None:
        _append(session, task=task, recipient_id=recipient, kind=kind,
                event_key=event_id, rule_version=TASK_RULE, current=current)


def _start_write(session: Session) -> None:
    connection = session.connection()
    if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")


def mark_read(session: Session, actor: AuthenticatedSubject, notification_id: str, *,
              expected_version: int, idempotency_key: str,
              now: datetime | None = None) -> Notification:
    _authorize(session, actor, "notifications.ack")
    current = _now(now)
    if not 1 <= expected_version <= 2147483646:
        raise HTTPException(422, "notification_expected_version_invalid")
    if not idempotency_key or len(idempotency_key) > 200:
        raise HTTPException(400, "idempotency_key_invalid")
    digest = sha256(json.dumps([actor.organization_id, actor.store_id,
        actor.subject_id, notification_id, expected_version],
        separators=(",", ":")).encode("utf-8")).hexdigest()

    def receipt() -> Notification | None:
        command = session.scalar(select(NotificationCommand).where(
            NotificationCommand.organization_id == actor.organization_id,
            NotificationCommand.store_id == actor.store_id,
            NotificationCommand.actor_id == actor.subject_id,
            NotificationCommand.idempotency_key == idempotency_key))
        if command is None:
            return None
        if command.payload_digest != digest or command.notification_id != notification_id:
            raise HTTPException(409, "idempotency_conflict")
        row = _visible_one(session, actor, notification_id, current)
        if row.version != command.response.get("version") or (
                row.read_at is None) != (command.response.get("read_at") is None):
            raise HTTPException(409, "notification_receipt_stale")
        return row

    previous = receipt()
    if previous is not None:
        return previous
    _start_write(session)
    try:
        with session.begin_nested():
            row = _visible_one(session, actor, notification_id, current)
            if row.version != expected_version or row.read_at is not None:
                raise HTTPException(409, "notification_version_conflict")
            changed = session.execute(update(Notification).where(
                Notification.id == row.id,
                Notification.organization_id == actor.organization_id,
                Notification.store_id == actor.store_id,
                Notification.recipient_id == actor.subject_id,
                Notification.version == expected_version,
                Notification.read_at.is_(None),
            ).values(read_at=current, version=expected_version + 1))
            if changed.rowcount != 1:
                raise HTTPException(409, "notification_version_conflict")
            session.expire(row)
            row = _visible_one(session, actor, notification_id, current)
            record_event(session, actor_id=actor.subject_id,
                organization_id=actor.organization_id, store_id=actor.store_id,
                action="notification.read", target_type="notification", target_id=row.id,
                idempotency_key="notification:read:" + sha256(
                    (actor.subject_id + ":" + idempotency_key).encode("utf-8")).hexdigest(),
                details={"expected_version": expected_version, "version": row.version})
            session.add(NotificationCommand(
                id=str(uuid4()), organization_id=actor.organization_id,
                store_id=actor.store_id, actor_id=actor.subject_id,
                idempotency_key=idempotency_key, payload_digest=digest,
                notification_id=row.id,
                response={"version": row.version, "read_at": row.read_at.isoformat()},
                created_at=current))
            session.flush()
        return row
    except IntegrityError:
        prior = receipt()
        if prior is not None:
            return prior
        raise HTTPException(409, "notification_command_conflict") from None


def reconcile_overdue(session: Session, actor: AuthenticatedSubject, *,
                      key: str, now: datetime | None = None, limit: int = 100) -> dict:
    """Explicit operator action; no autonomous escalation or outbound delivery.

    The operator cannot choose a recipient. Dedupe is tied to the exact due_at,
    assignee, task and rule version. Requires a dedicated permission.
    """
    _authorize(session, actor, "notifications.manage")
    current = _now(now)
    if not 1 <= limit <= 100:
        raise HTTPException(422, "notification_limit_invalid")
    if not key or len(key) > 200:
        raise HTTPException(400, "idempotency_key_invalid")
    audit_key = "notification:reconcile:" + sha256(
        (actor.subject_id + ":" + key).encode("utf-8")).hexdigest()
    previous = get_by_idempotency_key(session, actor.organization_id, actor.store_id, audit_key)
    if previous is not None:
        if previous.action != "notification.reconcile" or previous.details.get("limit") != limit:
            raise HTTPException(409, "idempotency_conflict")
        return {field: previous.details[field] for field in ("created", "scanned", "rule_version")}
    _start_write(session)
    scanned, created = 0, 0
    rows = session.scalars(select(Task).where(
        Task.organization_id == actor.organization_id,
        Task.store_id == actor.store_id,
        Task.assignee_id.is_not(None),
        Task.due_at <= current,
        Task.state.in_(("assigned", "in_progress")),
    ).order_by(Task.due_at.asc(), Task.id.asc()).limit(limit)).all()
    for task in rows:
        scanned += 1
        due = task.due_at
        if due is None:
            continue
        due_utc = due.replace(tzinfo=timezone.utc) if due.tzinfo is None else due.astimezone(timezone.utc)
        event_key = sha256(json.dumps([task.id, task.assignee_id,
            due_utc.isoformat(), OVERDUE_RULE], separators=(",", ":")).encode("utf-8")).hexdigest()
        if _append(session, task=task, recipient_id=task.assignee_id, kind="overdue",
                   event_key=event_key, rule_version=OVERDUE_RULE, current=current):
            created += 1
            record_event(session, actor_id=actor.subject_id,
                organization_id=actor.organization_id, store_id=actor.store_id,
                action="notification.overdue", target_type="task", target_id=task.id,
                idempotency_key="notification:overdue:" + event_key,
                details={"recipient_id": task.assignee_id, "rule_version": OVERDUE_RULE,
                         "due_at": due_utc.isoformat()})
    record_event(session, actor_id=actor.subject_id,
        organization_id=actor.organization_id, store_id=actor.store_id,
        action="notification.reconcile", target_type="notification_rule",
        target_id=OVERDUE_RULE, idempotency_key=audit_key,
        details={"limit": limit, "created": created, "scanned": scanned,
                 "rule_version": OVERDUE_RULE})
    session.flush()
    return {"created": created, "scanned": scanned, "rule_version": OVERDUE_RULE}


def escalate_without_approved_policy(*_args, **_kwargs):
    """No approved escalation map exists; fail closed instead of guessing a manager."""
    raise HTTPException(409, "notification_escalation_policy_required")
