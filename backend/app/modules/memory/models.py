from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, Integer, JSON, String, Text, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime


class PersonalMemory(Base):
    """Owner-scoped memory; never a shared fact or catalog override."""

    __tablename__ = "personal_memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    content: Mapped[str] = mapped_column(Text(), nullable=False)
    purpose: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    create_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    persistent: Mapped[bool] = mapped_column(default=False, nullable=False)
    retain_until_withdrawn: Mapped[bool] = mapped_column(default=False, server_default=false(), nullable=False)
    correction_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    correction_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    withdraw_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    version: Mapped[int] = mapped_column(Integer(), nullable=False)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_personal_memories_scope_id"),
        UniqueConstraint("organization_id", "store_id", "owner_id", "idempotency_key",
                         name="uq_personal_memories_owner_idem"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_personal_memories_store_scope", ondelete="RESTRICT"),
        CheckConstraint("version > 0", name="ck_personal_memories_version"),
        CheckConstraint("(retain_until_withdrawn AND persistent AND expires_at IS NULL) OR "
                        "(NOT retain_until_withdrawn AND expires_at IS NOT NULL)",
                        name="ck_personal_memories_retention"),
        CheckConstraint("length(content) BETWEEN 1 AND 5000", name="ck_personal_memories_content"),
        Index("ix_personal_memories_owner", "organization_id", "store_id", "owner_id", "active"),
    )


class MemoryCommand(Base):
    __tablename__ = "memory_commands"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "actor_id", "operation", "idempotency_key",
                         name="uq_memory_commands_identity"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                             name="fk_memory_commands_scope", ondelete="RESTRICT"),
    )
