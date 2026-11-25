from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.modules.audit.models import AuditEvent
from backend.app.modules.audit.repository import append_event, get_by_idempotency_key, get_cursor_event, list_events_after
from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.identity.models import Subject
from backend.app.modules.identity.service import authorize_scope


def record_event(
    session: Session,
    *,
    actor_id: str,
    organization_id: str,
    store_id: str,
    action: str,
    target_type: str,
    target_id: str,
    idempotency_key: str,
    details: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> AuditEvent:
    """Append one audit event in the caller's transaction; duplicate keys reuse the event."""
    if not all((actor_id, organization_id, store_id, action, target_type, target_id, idempotency_key)):
        raise ValueError("audit event identity fields must be non-empty")
    if len(idempotency_key) > 200:
        raise ValueError("idempotency_key exceeds 200 characters")
    timestamp = occurred_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("occurred_at must include a timezone")
    timestamp = timestamp.astimezone(timezone.utc)
    existing = get_by_idempotency_key(session, organization_id, store_id, idempotency_key)
    if existing is not None:
        proposed = (actor_id, action, target_type, target_id, details or {})
        persisted = (existing.actor_id, existing.action, existing.target_type, existing.target_id, existing.details)
        if proposed != persisted:
            raise ValueError("idempotency key was already used for a different audit event")
        return existing
    event = AuditEvent(
        id=str(uuid4()), organization_id=organization_id, store_id=store_id,
        actor_id=actor_id, action=action, target_type=target_type, target_id=target_id,
        idempotency_key=idempotency_key, occurred_at=timestamp,
        details=details or {},
    )
    # The unique scope key protects concurrent inserts; a racing caller handles
    # the resulting integrity error at its transaction boundary.
    return append_event(session, event)


def query_events(
    session: Session,
    actor: AuthenticatedSubject,
    *,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[AuditEvent], str | None]:
    """Read an actor-scoped audit page; append/write behavior stays unchanged."""
    authorize_scope(session, actor, organization_id=actor.organization_id, store_id=actor.store_id,
                    permission="audit.read")
    subject_type = session.scalar(select(Subject.subject_type).where(
        Subject.organization_id == actor.organization_id, Subject.id == actor.subject_id))
    if subject_type != "person":
        raise HTTPException(status_code=403, detail="audit_person_required")
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="audit_limit_invalid")

    cursor_event = None
    if cursor is not None:
        cursor_event = get_cursor_event(session, actor.organization_id, actor.store_id, cursor)
        if cursor_event is None:
            raise HTTPException(status_code=400, detail="audit_cursor_invalid")

    rows = list_events_after(
        session, actor.organization_id, actor.store_id,
        cursor_at=cursor_event.occurred_at if cursor_event else None,
        cursor_id=cursor_event.id if cursor_event else None,
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = items[-1].id if has_more and items else None
    return items, next_cursor
