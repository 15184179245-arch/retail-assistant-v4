from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    requester_id: Mapped[str] = mapped_column(String(36), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(100), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(200), nullable=False)
    action_key: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="requested", server_default="requested")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    expired_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.current_timestamp())
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_approval_requests_scope_id"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_approval_requests_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "requester_id"], ["subjects.organization_id", "subjects.id"], name="fk_approval_requests_requester_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "decided_by"], ["subjects.organization_id", "subjects.id"], name="fk_approval_requests_decider_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "cancelled_by"], ["subjects.organization_id", "subjects.id"], name="fk_approval_requests_canceller_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "expired_by"], ["subjects.organization_id", "subjects.id"], name="fk_approval_requests_expirer_scope", ondelete="RESTRICT"),
        CheckConstraint("state IN ('requested', 'approved', 'rejected', 'cancelled', 'expired')", name="ck_approval_requests_state"),
        CheckConstraint("length(subject_type) > 0 AND length(subject_id) > 0 AND length(action_key) > 0", name="ck_approval_requests_subject_action_nonempty"),
        Index("ix_approval_requests_inbox", "organization_id", "store_id", "state", "requested_at"),
    )


class ApprovalIdempotency(Base):
    __tablename__ = "approval_idempotency"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.current_timestamp())
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "idempotency_key", name="uq_approval_idempotency_scope_key"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_approval_idempotency_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "approval_id"], ["approval_requests.organization_id", "approval_requests.store_id", "approval_requests.id"], name="fk_approval_idempotency_request_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "actor_id"], ["subjects.organization_id", "subjects.id"], name="fk_approval_idempotency_actor_scope", ondelete="RESTRICT"),
        CheckConstraint("operation IN ('request', 'decision', 'cancel', 'expire')", name="ck_approval_idempotency_operation"),
        Index("ix_approval_idempotency_approval", "organization_id", "store_id", "approval_id"),
    )
