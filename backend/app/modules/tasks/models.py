from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    proposal_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reviewer_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("subjects.id"), nullable=True)
    decision_state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="suggested", server_default="suggested")
    accepted_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assigned_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assignee_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.current_timestamp())

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_tasks_scope_id"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_tasks_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "created_by"], ["subjects.organization_id", "subjects.id"], name="fk_tasks_creator_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "accepted_by"], ["subjects.organization_id", "subjects.id"], name="fk_tasks_acceptor_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "assigned_by"], ["subjects.organization_id", "subjects.id"], name="fk_tasks_assigner_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "assignee_id"], ["subjects.organization_id", "subjects.id"], name="fk_tasks_assignee_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "started_by"], ["subjects.organization_id", "subjects.id"], name="fk_tasks_starter_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "verified_by"], ["subjects.organization_id", "subjects.id"], name="fk_tasks_verifier_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "closed_by"], ["subjects.organization_id", "subjects.id"], name="fk_tasks_closer_scope", ondelete="RESTRICT"),
        CheckConstraint("state IN ('suggested', 'accepted', 'assigned', 'in_progress', 'verified', 'closed')", name="ck_tasks_state"),
        CheckConstraint("source IN ('agent', 'human', 'schedule')", name="ck_tasks_source"),
        CheckConstraint("decision_state IN ('pending', 'accepted', 'rejected', 'deferred', 'info_requested')", name="ck_tasks_decision_state"),
        CheckConstraint("length(title) > 0", name="ck_tasks_title_nonempty"),
        Index("ix_tasks_inbox", "organization_id", "store_id", "state", "created_at"),
        Index("ix_tasks_assignee", "organization_id", "store_id", "assignee_id", "state"),
        Index("ix_tasks_reviewer", "organization_id", "store_id", "reviewer_id"),
    )


class TaskEvent(Base):
    __tablename__ = "task_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_state: Mapped[str] = mapped_column(String(16), nullable=False)
    decision_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    responsible_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id", "task_id"], ["tasks.organization_id", "tasks.store_id", "tasks.id"], name="fk_task_events_task_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "actor_id"], ["subjects.organization_id", "subjects.id"], name="fk_task_events_actor_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "responsible_id"], ["subjects.organization_id", "subjects.id"], name="fk_task_events_responsible_scope", ondelete="RESTRICT"),
        CheckConstraint("to_state IN ('suggested', 'accepted', 'assigned', 'in_progress', 'verified', 'closed')", name="ck_task_events_to_state"),
        CheckConstraint("from_state IS NULL OR from_state IN ('suggested', 'accepted', 'assigned', 'in_progress', 'verified', 'closed')", name="ck_task_events_from_state"),
        CheckConstraint("decision_state IS NULL OR decision_state IN ('pending', 'accepted', 'rejected', 'deferred', 'info_requested')", name="ck_task_events_decision_state"),
        Index("ix_task_events_timeline", "organization_id", "store_id", "task_id", "occurred_at"),
    )


class TaskIdempotency(Base):
    __tablename__ = "task_idempotency"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.current_timestamp())
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "idempotency_key", name="uq_task_idempotency_scope_key"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_task_idempotency_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "task_id"], ["tasks.organization_id", "tasks.store_id", "tasks.id"], name="fk_task_idempotency_task_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "actor_id"], ["subjects.organization_id", "subjects.id"], name="fk_task_idempotency_actor_scope", ondelete="RESTRICT"),
        CheckConstraint("operation IN ('create', 'accept', 'assign', 'start', 'verify', 'close', 'decide')", name="ck_task_idempotency_operation"),
        Index("ix_task_idempotency_task", "organization_id", "store_id", "task_id"),
    )
