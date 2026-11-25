"""SQLAlchemy ORM models for WSP-001 workspace artifacts, ops, and thread bindings."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime


class WorkspaceArtifact(Base):
    """Store- and employee-scoped collaborative working artifact."""

    __tablename__ = "workspace_artifacts"
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_workspace_artifacts_scope_id"),
        ForeignKeyConstraint(
            ["organization_id", "store_id"],
            ["stores.organization_id", "stores.id"],
            name="fk_workspace_artifacts_store_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "owner_subject_id"],
            ["subjects.organization_id", "subjects.id"],
            name="fk_workspace_artifacts_owner_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "kind IN ('quote_draft', 'purchase_draft', 'stocktake_review', 'receiving_batch', 'work_item_sheet')",
            name="ck_workspace_artifacts_kind",
        ),
        CheckConstraint(
            "status IN ('draft', 'submitted', 'archived')",
            name="ck_workspace_artifacts_status",
        ),
        CheckConstraint("version >= 1", name="ck_workspace_artifacts_version"),
        Index("ix_workspace_artifacts_thread_scope", "organization_id", "store_id", "thread_id"),
        Index("ix_workspace_artifacts_owner_scope", "organization_id", "store_id", "owner_subject_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    locked_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    pending_suggestions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    submitted_receipt_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    submit_idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class WorkspaceArtifactOp(Base):
    """Append-only operation log supporting L1 locks, AI suggestions, and partial undo."""

    __tablename__ = "workspace_artifact_ops"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "store_id",
            "artifact_id",
            "op_type",
            "idempotency_key",
            name="uq_workspace_artifact_ops_idem",
        ),
        ForeignKeyConstraint(
            ["organization_id", "store_id", "artifact_id"],
            [
                "workspace_artifacts.organization_id",
                "workspace_artifacts.store_id",
                "workspace_artifacts.id",
            ],
            name="fk_workspace_artifact_ops_artifact_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "store_id"],
            ["stores.organization_id", "stores.id"],
            name="fk_workspace_artifact_ops_store_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint("actor_type IN ('human', 'agent')", name="ck_workspace_artifact_ops_actor_type"),
        CheckConstraint(
            "op_type IN ('create', 'patch', 'undo', 'lock', 'unlock', 'accept_suggestion', 'ignore_suggestion', 'submit')",
            name="ck_workspace_artifact_ops_type",
        ),
        Index(
            "ix_workspace_artifact_ops_artifact_created",
            "organization_id",
            "store_id",
            "artifact_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version_before: Mapped[int] = mapped_column(Integer, nullable=False)
    version_after: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    op_type: Mapped[str] = mapped_column(String(32), nullable=False)
    patch: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    before_values: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    applied_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    skipped_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    undo_of_op_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class WorkspaceThreadBinding(Base):
    """Cross-device thread workspace state tracking open artifacts and active editor hint."""

    __tablename__ = "workspace_thread_bindings"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "store_id",
            "thread_id",
            name="uq_workspace_thread_bindings_scope_thread",
        ),
        ForeignKeyConstraint(
            ["organization_id", "store_id"],
            ["stores.organization_id", "stores.id"],
            name="fk_workspace_thread_bindings_store_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "subject_id"],
            ["subjects.organization_id", "subjects.id"],
            name="fk_workspace_thread_bindings_subject_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version >= 1", name="ck_workspace_thread_bindings_version"),
        Index(
            "ix_workspace_thread_bindings_subject_scope",
            "organization_id",
            "store_id",
            "subject_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    open_objects: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    active_object_ref: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    last_editor_subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_editor_device: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_edited_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class WorkspaceThreadEvent(Base):
    """Cursor-replayable workspace, co-edit, and generative block events per thread."""

    __tablename__ = "workspace_thread_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "store_id"],
            ["stores.organization_id", "stores.id"],
            name="fk_workspace_thread_events_store_scope",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_workspace_thread_events_thread_seq",
            "organization_id",
            "store_id",
            "thread_id",
            "seq",
        ),
    )

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
