from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class AuditEvent(Base):
    """Append-only through the audit service API; direct database writes remain privileged."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    target_type: Mapped[str] = mapped_column(String(120), nullable=False)
    target_id: Mapped[str] = mapped_column(String(200), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.current_timestamp())

    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_audit_events_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "actor_id"], ["subjects.organization_id", "subjects.id"], name="fk_audit_events_actor_scope", ondelete="RESTRICT"),
        Index("ix_audit_events_scope_time", "organization_id", "store_id", "occurred_at"),
        Index("uq_audit_events_idempotency", "organization_id", "store_id", "idempotency_key", unique=True),
    )
