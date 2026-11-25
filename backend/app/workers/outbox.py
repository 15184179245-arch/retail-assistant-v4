"""Database outbox primitives. No network sender or business-success inference lives here."""

from datetime import datetime, timedelta, timezone
import re
from typing import Any
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, JSON, String, Text, select, update, and_, or_
from sqlalchemy.orm import Mapped, Session, mapped_column

from backend.app.db.base import Base

_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def _as_utc(value: datetime) -> datetime:
    """SQLite drops timezone metadata on read; interpret stored timestamps as UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _normalize_input_time(value: datetime | None, field: str) -> datetime:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return timestamp.astimezone(timezone.utc)


_MAX_EVENT_TYPES = 32
_MAX_EVENT_TYPE_LENGTH = 120
_MAX_SCOPE_ID_LENGTH = 36


def validate_claim_filters(
    *,
    event_types: tuple[str, ...] | None = None,
    organization_id: str | None = None,
    store_id: str | None = None,
) -> None:
    """Reject malformed claim filters before any statement reaches the database.

    Filtering is a claim boundary, not authorization: callers must still
    authenticate and derive scope/event types from a verified principal. The
    validation never touches the session, so it also fails fast for an unbound
    ``Session``.
    """
    if (organization_id is None) != (store_id is None):
        raise ValueError("organization_id and store_id must be provided together")
    for field, value in (("organization_id", organization_id), ("store_id", store_id)):
        if value is None:
            continue
        if not isinstance(value, str) or not 1 <= len(value) <= _MAX_SCOPE_ID_LENGTH:
            raise ValueError(f"{field} must be a string of 1..{_MAX_SCOPE_ID_LENGTH} characters")
        if value != value.strip():
            raise ValueError(f"{field} must not have leading or trailing whitespace")
    if event_types is None:
        return
    if not isinstance(event_types, tuple):
        raise ValueError("event_types must be a tuple of event type strings")
    if len(event_types) > _MAX_EVENT_TYPES:
        raise ValueError(f"event_types must contain at most {_MAX_EVENT_TYPES} entries")
    seen: set[str] = set()
    for event_type in event_types:
        if not isinstance(event_type, str) or not 1 <= len(event_type) <= _MAX_EVENT_TYPE_LENGTH:
            raise ValueError(f"each event type must be a string of 1..{_MAX_EVENT_TYPE_LENGTH} characters")
        if event_type != event_type.strip():
            raise ValueError("event types must not have leading or trailing whitespace")
        if event_type in seen:
            raise ValueError("event_types must not contain duplicates")
        seen.add(event_type)


def _claim_filter_clauses(
    event_types: tuple[str, ...] | None,
    organization_id: str | None,
    store_id: str | None,
) -> list[Any]:
    """SQL predicates shared by every claiming/dead-letter statement."""
    clauses: list[Any] = []
    if event_types is not None:
        clauses.append(OutboxEvent.event_type.in_(event_types))
    if organization_id is not None:
        clauses.append(OutboxEvent.organization_id == organization_id)
        clauses.append(OutboxEvent.store_id == store_id)
    return clauses


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_receipt: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_outbox_events_store_scope", ondelete="RESTRICT"),
        CheckConstraint("status IN ('pending', 'leased', 'retry', 'dead', 'delivered')", name="ck_outbox_events_status"),
        CheckConstraint("attempts >= 0", name="ck_outbox_events_attempts_nonnegative"),
        Index("uq_outbox_scope_idempotency", "organization_id", "store_id", "idempotency_key", unique=True),
        Index("ix_outbox_claim", "status", "available_at", "lease_until"),
    )


def enqueue(
    session: Session,
    *,
    organization_id: str,
    store_id: str,
    event_type: str,
    idempotency_key: str,
    payload: dict[str, Any],
    event_id: str | None = None,
    now: datetime | None = None,
) -> OutboxEvent:
    """Stage an event in the caller's transaction; caller owns commit/rollback."""
    if not all((organization_id, store_id, event_type, idempotency_key)):
        raise ValueError("outbox identity fields must be non-empty")
    if len(idempotency_key) > 200:
        raise ValueError("idempotency_key exceeds 200 characters")
    existing = session.scalar(select(OutboxEvent).where(
        OutboxEvent.organization_id == organization_id,
        OutboxEvent.store_id == store_id,
        OutboxEvent.idempotency_key == idempotency_key,
    ))
    if existing is not None:
        if existing.event_type != event_type or existing.payload != payload:
            raise ValueError("idempotency key was already used for a different outbox event")
        return existing
    timestamp = _normalize_input_time(now, "now")
    event = OutboxEvent(
        id=event_id or str(uuid4()), organization_id=organization_id, store_id=store_id,
        event_type=event_type, idempotency_key=idempotency_key, payload=payload,
        status="pending", attempts=0, available_at=timestamp, created_at=timestamp,
    )
    session.add(event)
    session.flush()
    return event


def claim_one(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: int = 30,
    max_attempts: int = 8,
    now: datetime | None = None,
    event_types: tuple[str, ...] | None = None,
    organization_id: str | None = None,
    store_id: str | None = None,
) -> OutboxEvent | None:
    """Lease one eligible event. Expired leases are recoverable after process loss.

    ``event_types``/``organization_id``/``store_id`` narrow the claim at the SQL
    boundary, including the expired-exhausted dead-letter pass: non-matching
    rows are never read into Python and never have status, attempts,
    last_error, owner, or lease_until changed. ``None`` keeps the original
    all-event behaviour; an empty ``event_types`` tuple claims and dead-letters
    nothing and issues no statement at all.
    """
    if not worker_id.strip() or lease_seconds <= 0 or max_attempts <= 0:
        raise ValueError("worker_id, positive lease_seconds, and positive max_attempts are required")
    validate_claim_filters(event_types=event_types, organization_id=organization_id, store_id=store_id)
    timestamp = _normalize_input_time(now, "now")
    if event_types is not None and not event_types:
        return None
    filters = _claim_filter_clauses(event_types, organization_id, store_id)
    # A worker that repeatedly dies without reporting failure must still reach
    # the dead-letter state after its bounded lease attempts.
    expired_exhausted = session.execute(select(
        OutboxEvent.id, OutboxEvent.lease_until, OutboxEvent.attempts,
    ).where(
        OutboxEvent.status == "leased",
        OutboxEvent.lease_until <= timestamp,
        OutboxEvent.attempts >= max_attempts,
        *filters,
    ).with_for_update(skip_locked=True)).all()
    for abandoned_id, observed_lease_until, observed_attempts in expired_exhausted:
        session.execute(update(OutboxEvent).where(
            OutboxEvent.id == abandoned_id,
            OutboxEvent.status == "leased",
            OutboxEvent.lease_until == observed_lease_until,
            OutboxEvent.lease_until <= timestamp,
            OutboxEvent.attempts == observed_attempts,
            OutboxEvent.attempts >= max_attempts,
            *filters,
        ).values(
            status="dead",
            last_error="LEASE_EXPIRED_MAX_ATTEMPTS",
            lease_owner=None,
            lease_until=None,
        ).execution_options(synchronize_session=False))
    if expired_exhausted:
        session.flush()
    eligible = or_(
        and_(OutboxEvent.status == "pending", OutboxEvent.available_at <= timestamp),
        and_(OutboxEvent.status == "retry", OutboxEvent.available_at <= timestamp),
        and_(OutboxEvent.status == "leased", OutboxEvent.lease_until <= timestamp, OutboxEvent.attempts < max_attempts),
    )
    event = session.scalar(select(OutboxEvent).where(eligible, *filters).order_by(OutboxEvent.created_at, OutboxEvent.id).with_for_update(skip_locked=True).limit(1))
    if event is None:
        return None
    prior_status = event.status
    conditions = [
        OutboxEvent.id == event.id,
        OutboxEvent.status == prior_status,
        OutboxEvent.attempts == event.attempts,
    ]
    if prior_status == "leased":
        # Compare the exact expired lease version. A concurrent re-lease changes
        # owner/until/attempts and makes this update a no-op.
        conditions.extend((
            OutboxEvent.lease_owner == event.lease_owner,
            OutboxEvent.lease_until == event.lease_until,
            OutboxEvent.lease_until <= timestamp,
            OutboxEvent.attempts < max_attempts,
        ))
    else:
        conditions.extend((
            OutboxEvent.available_at == event.available_at,
            OutboxEvent.available_at <= timestamp,
        ))
    result = session.execute(update(OutboxEvent).where(*conditions, *filters).values({
        OutboxEvent.status: "leased",
        OutboxEvent.lease_owner: worker_id,
        OutboxEvent.lease_until: timestamp + timedelta(seconds=lease_seconds),
        OutboxEvent.attempts: OutboxEvent.attempts + 1,
    }).execution_options(synchronize_session=False))
    if not result.rowcount:
        session.expire(event)
        return None
    session.flush()
    session.expire(event)
    return event


def record_failure(
    session: Session,
    *,
    event_id: str,
    worker_id: str,
    error: str,
    max_attempts: int = 8,
    now: datetime | None = None,
) -> OutboxEvent:
    """Record failure only under an unexpired lease, then retry or dead-letter."""
    if not isinstance(error, str) or _ERROR_CODE.fullmatch(error) is None:
        raise ValueError("error must be a stable error code")
    if max_attempts <= 0:
        raise ValueError("positive max_attempts is required")
    timestamp = _normalize_input_time(now, "now")
    event = session.get(OutboxEvent, event_id)
    if event is None or event.status != "leased" or event.lease_owner != worker_id or event.lease_until is None or _as_utc(event.lease_until) <= timestamp:
        raise ValueError("active unexpired lease not owned by worker")
    if event.attempts >= max_attempts:
        values = {"status": "dead"}
    else:
        bounded_exponent = min(event.attempts, 9)
        values = {"status": "retry", "available_at": timestamp + timedelta(seconds=min(2 ** bounded_exponent, 300))}
    result = session.execute(update(OutboxEvent).where(
        OutboxEvent.id == event_id,
        OutboxEvent.status == "leased",
        OutboxEvent.lease_owner == worker_id,
        OutboxEvent.lease_until == event.lease_until,
        OutboxEvent.lease_until > timestamp,
        OutboxEvent.attempts == event.attempts,
    ).values(**values, last_error=error, lease_owner=None, lease_until=None).execution_options(synchronize_session=False))
    if not result.rowcount:
        raise ValueError("active unexpired lease not owned by worker")
    session.expire(event)
    session.refresh(event)
    return event


def acknowledge_receipt(
    session: Session,
    *,
    event_id: str,
    worker_id: str,
    external_receipt: str,
    now: datetime | None = None,
) -> OutboxEvent:
    """Mark delivered with a receipt only under the worker's unexpired lease."""
    if not external_receipt.strip():
        raise ValueError("external receipt is required")
    timestamp = _normalize_input_time(now, "now")
    event = session.get(OutboxEvent, event_id)
    if event is None or event.status != "leased" or event.lease_owner != worker_id or event.lease_until is None or _as_utc(event.lease_until) <= timestamp:
        raise ValueError("active unexpired lease not owned by worker")
    result = session.execute(update(OutboxEvent).where(
        OutboxEvent.id == event_id,
        OutboxEvent.status == "leased",
        OutboxEvent.lease_owner == worker_id,
        OutboxEvent.lease_until == event.lease_until,
        OutboxEvent.lease_until > timestamp,
        OutboxEvent.attempts == event.attempts,
    ).values(
        status="delivered",
        external_receipt=external_receipt[:500],
        delivered_at=timestamp,
        lease_owner=None,
        lease_until=None,
    ).execution_options(synchronize_session=False))
    if not result.rowcount:
        raise ValueError("active unexpired lease not owned by worker")
    session.expire(event)
    session.refresh(event)
    return event
