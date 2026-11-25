"""NOT-001: durable, store-scoped inbox and idempotent read receipts."""
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    recipient_id: Mapped[str] = mapped_column(String(36), nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_event_key: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(40), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_notifications_scope_id"),
        UniqueConstraint("organization_id", "store_id", "recipient_id",
                         "source_event_key", "kind", name="uq_notifications_event_recipient"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_notifications_store", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "recipient_id"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_notifications_recipient", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "task_id"],
                             ["tasks.organization_id", "tasks.store_id", "tasks.id"],
                             name="fk_notifications_task", ondelete="RESTRICT"),
        CheckConstraint("kind IN ('review_requested','assigned','overdue','manual_check','failure')",
                        name="ck_notifications_kind"),
        CheckConstraint("version > 0", name="ck_notifications_version"),
        Index("ix_notifications_inbox", "organization_id", "store_id",
              "recipient_id", "created_at"),
    )


class NotificationCommand(Base):
    __tablename__ = "notification_commands"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    notification_id: Mapped[str] = mapped_column(String(36), nullable=False)
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "actor_id",
                         "idempotency_key", name="uq_notification_commands_actor_key"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_notification_commands_store", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "actor_id"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_notification_commands_actor", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "notification_id"],
                             ["notifications.organization_id", "notifications.store_id", "notifications.id"],
                             name="fk_notification_commands_notification", ondelete="RESTRICT"),
    )
