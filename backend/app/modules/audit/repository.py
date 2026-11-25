from datetime import datetime

from sqlalchemy import inspect, or_, select
from sqlalchemy.orm import Session

from backend.app.modules.audit.models import AuditEvent


def append_event(session: Session, event: AuditEvent) -> AuditEvent:
    """Stage a new event in the caller's transaction; never commits it."""
    if not inspect(event).transient:
        raise ValueError("audit repository accepts new events only")
    session.add(event)
    session.flush()
    return event


def get_by_idempotency_key(session: Session, organization_id: str, store_id: str, key: str) -> AuditEvent | None:
    return session.scalar(select(AuditEvent).where(
        AuditEvent.organization_id == organization_id,
        AuditEvent.store_id == store_id,
        AuditEvent.idempotency_key == key,
    ))


def get_cursor_event(session: Session, organization_id: str, store_id: str, event_id: str) -> AuditEvent | None:
    """Resolve cursors only inside the caller's organization and store scope."""
    return session.scalar(select(AuditEvent).where(
        AuditEvent.organization_id == organization_id,
        AuditEvent.store_id == store_id,
        AuditEvent.id == event_id,
    ))


def list_events_after(
    session: Session,
    organization_id: str,
    store_id: str,
    *,
    cursor_at: datetime | None,
    cursor_id: str | None,
    limit: int,
) -> list[AuditEvent]:
    statement = select(AuditEvent).where(
        AuditEvent.organization_id == organization_id,
        AuditEvent.store_id == store_id,
    )
    if cursor_at is not None and cursor_id is not None:
        statement = statement.where(or_(
            AuditEvent.occurred_at < cursor_at,
            (AuditEvent.occurred_at == cursor_at) & (AuditEvent.id < cursor_id),
        ))
    statement = statement.order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc()).limit(limit)
    return list(session.scalars(statement))
