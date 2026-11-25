from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime
from backend.app.modules.knowledge import projection_models as _projection_models  # noqa: F401


class KnowledgeEntry(Base):
    """One knowledge item; its revisions preserve every correction."""

    __tablename__ = "knowledge_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False, default="store")
    scope_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending_review")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    current_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    submitted_by: Mapped[str] = mapped_column(String(36), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    withdrawn_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    withdraw_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_knowledge_entries_scope_id"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                             name="fk_knowledge_entries_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "submitted_by"], ["subjects.organization_id", "subjects.id"],
                             name="fk_knowledge_entries_submitter_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "approved_by"], ["subjects.organization_id", "subjects.id"],
                             name="fk_knowledge_entries_approver_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "withdrawn_by"], ["subjects.organization_id", "subjects.id"],
                             name="fk_knowledge_entries_withdrawer_scope", ondelete="RESTRICT"),
        CheckConstraint("status IN ('pending_review', 'approved', 'rejected', 'withdrawn', 'expired')",
                        name="ck_knowledge_entries_status"),
        CheckConstraint("scope_type IN ('store', 'product', 'category', 'global')",
                        name="ck_knowledge_entries_scope_type"),
        CheckConstraint("version > 0", name="ck_knowledge_entries_version_positive"),
        CheckConstraint("updated_at >= created_at", name="ck_knowledge_entries_updated_after_created"),
        Index("ix_knowledge_entries_scope_status", "organization_id", "store_id", "status", "expires_at"),
    )


class KnowledgeRevision(Base):
    """Immutable snapshot of a submission/correction; approval references one."""

    __tablename__ = "knowledge_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    entry_id: Mapped[str] = mapped_column(String(36), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text(), nullable=False)
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    submitted_by: Mapped[str] = mapped_column(String(36), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("entry_id", "version", name="uq_knowledge_revisions_version"),
        UniqueConstraint("organization_id", "store_id", "id", name="uq_knowledge_revisions_scope_id"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                             name="fk_knowledge_revisions_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["entry_id"], ["knowledge_entries.id"],
                             name="fk_knowledge_revisions_entry", ondelete="CASCADE"),
        ForeignKeyConstraint(["organization_id", "submitted_by"], ["subjects.organization_id", "subjects.id"],
                             name="fk_knowledge_revisions_submitter_scope", ondelete="RESTRICT"),
        CheckConstraint("version > 0", name="ck_knowledge_revisions_version_positive"),
        CheckConstraint("source_type IN ('manual', 'file', 'link', 'import')",
                        name="ck_knowledge_revisions_source_type"),
        CheckConstraint("length(trim(title)) > 0 AND length(trim(body)) > 0",
                        name="ck_knowledge_revisions_content_nonempty"),
        Index("ix_knowledge_revisions_entry", "entry_id", "version"),
    )


class KnowledgeCommand(Base):
    """Immutable completed HTTP receipt; reservation and effect share a transaction."""
    __tablename__ = "knowledge_commands"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "actor_id", "operation", "idempotency_key",
                         name="uq_knowledge_commands_scope_key"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                             name="fk_knowledge_commands_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "actor_id"], ["subjects.organization_id", "subjects.id"],
                             name="fk_knowledge_commands_actor_scope", ondelete="RESTRICT"),
        CheckConstraint("operation IN ('submit', 'revise', 'review', 'withdraw')", name="ck_knowledge_commands_operation"),
    )


class KnowledgeRevisionAccess(Base):
    """Trusted provenance; sensitivity may tighten, never declassify a revision."""
    __tablename__ = "knowledge_revision_access"
    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    origin_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    origin_memory_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    origin_memory_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    classification: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    access_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id", "revision_id"],
                             ["knowledge_revisions.organization_id", "knowledge_revisions.store_id", "knowledge_revisions.id"],
                             name="fk_knowledge_access_revision_scope", ondelete="RESTRICT"),
        CheckConstraint("origin_kind IN ('ordinary','memory','legacy_record','unknown')", name="ck_knowledge_access_origin"),
        CheckConstraint("classification IN ('public','restricted','hold')", name="ck_knowledge_access_class"),
        CheckConstraint("policy_version = 1 AND access_epoch > 0", name="ck_knowledge_access_epoch"),
        CheckConstraint("origin_memory_version IS NULL OR origin_memory_version > 0", name="ck_knowledge_access_memory_version"),
        CheckConstraint("(origin_kind = 'memory' AND origin_memory_id IS NOT NULL AND length(trim(origin_memory_id)) > 0) OR "
                        "(origin_kind <> 'memory' AND origin_memory_id IS NULL AND origin_memory_version IS NULL)",
                        name="ck_knowledge_access_memory_origin"),
        CheckConstraint("origin_kind <> 'unknown' OR classification = 'hold'", name="ck_knowledge_access_unknown_hold"),
    )
