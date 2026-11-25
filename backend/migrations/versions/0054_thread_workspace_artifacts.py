"""Add workspace_artifacts, workspace_artifact_ops, workspace_thread_bindings, and workspace_thread_events (WSP-001/WSP-002).

Revision ID: 0054_thread_workspace_artifacts
Revises: 0053_knowledge_revision_access
Create Date: 2026-11-16
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0054_thread_workspace_artifacts"
down_revision = "0053_knowledge_revision_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_artifacts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("owner_subject_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("locked_fields", sa.JSON(), nullable=False),
        sa.Column("pending_suggestions", sa.JSON(), nullable=False),
        sa.Column("submitted_receipt_ref", sa.String(128), nullable=True),
        sa.Column("submit_idempotency_key", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_workspace_artifacts_scope_id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "store_id"],
            ["stores.organization_id", "stores.id"],
            name="fk_workspace_artifacts_store_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "owner_subject_id"],
            ["subjects.organization_id", "subjects.id"],
            name="fk_workspace_artifacts_owner_scope",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "kind IN ('quote_draft', 'purchase_draft', 'stocktake_review', 'receiving_batch', 'work_item_sheet')",
            name="ck_workspace_artifacts_kind",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted', 'archived')",
            name="ck_workspace_artifacts_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_workspace_artifacts_version"),
    )
    op.create_index(
        "ix_workspace_artifacts_thread_scope",
        "workspace_artifacts",
        ["organization_id", "store_id", "thread_id"],
    )
    op.create_index(
        "ix_workspace_artifacts_owner_scope",
        "workspace_artifacts",
        ["organization_id", "store_id", "owner_subject_id"],
    )

    op.create_table(
        "workspace_artifact_ops",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("artifact_id", sa.String(64), nullable=False),
        sa.Column("version_before", sa.Integer(), nullable=False),
        sa.Column("version_after", sa.Integer(), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_subject_id", sa.String(64), nullable=False),
        sa.Column("op_type", sa.String(32), nullable=False),
        sa.Column("patch", sa.JSON(), nullable=False),
        sa.Column("before_values", sa.JSON(), nullable=False),
        sa.Column("applied_fields", sa.JSON(), nullable=False),
        sa.Column("skipped_fields", sa.JSON(), nullable=False),
        sa.Column("undo_of_op_id", sa.String(64), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "organization_id",
            "store_id",
            "artifact_id",
            "op_type",
            "idempotency_key",
            name="uq_workspace_artifact_ops_idem",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "store_id", "artifact_id"],
            [
                "workspace_artifacts.organization_id",
                "workspace_artifacts.store_id",
                "workspace_artifacts.id",
            ],
            name="fk_workspace_artifact_ops_artifact_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "store_id"],
            ["stores.organization_id", "stores.id"],
            name="fk_workspace_artifact_ops_store_scope",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("actor_type IN ('human', 'agent')", name="ck_workspace_artifact_ops_actor_type"),
        sa.CheckConstraint(
            "op_type IN ('create', 'patch', 'undo', 'lock', 'unlock', 'accept_suggestion', 'ignore_suggestion', 'submit')",
            name="ck_workspace_artifact_ops_type",
        ),
    )
    op.create_index(
        "ix_workspace_artifact_ops_artifact_created",
        "workspace_artifact_ops",
        ["organization_id", "store_id", "artifact_id", "created_at"],
    )

    op.create_table(
        "workspace_thread_bindings",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("open_objects", sa.JSON(), nullable=False),
        sa.Column("active_object_ref", sa.JSON(), nullable=True),
        sa.Column("last_editor_subject_id", sa.String(64), nullable=True),
        sa.Column("last_editor_device", sa.String(32), nullable=True),
        sa.Column("last_edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "organization_id",
            "store_id",
            "thread_id",
            name="uq_workspace_thread_bindings_scope_thread",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "store_id"],
            ["stores.organization_id", "stores.id"],
            name="fk_workspace_thread_bindings_store_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "subject_id"],
            ["subjects.organization_id", "subjects.id"],
            name="fk_workspace_thread_bindings_subject_scope",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("version >= 1", name="ck_workspace_thread_bindings_version"),
    )
    op.create_index(
        "ix_workspace_thread_bindings_subject_scope",
        "workspace_thread_bindings",
        ["organization_id", "store_id", "subject_id"],
    )

    op.create_table(
        "workspace_thread_events",
        sa.Column("seq", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(64), nullable=False, unique=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("artifact_id", sa.String(64), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "store_id"],
            ["stores.organization_id", "stores.id"],
            name="fk_workspace_thread_events_store_scope",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_workspace_thread_events_thread_seq",
        "workspace_thread_events",
        ["organization_id", "store_id", "thread_id", "seq"],
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_thread_events_thread_seq", table_name="workspace_thread_events")
    op.drop_table("workspace_thread_events")

    op.drop_index("ix_workspace_thread_bindings_subject_scope", table_name="workspace_thread_bindings")
    op.drop_table("workspace_thread_bindings")

    op.drop_index("ix_workspace_artifact_ops_artifact_created", table_name="workspace_artifact_ops")
    op.drop_table("workspace_artifact_ops")

    op.drop_index("ix_workspace_artifacts_owner_scope", table_name="workspace_artifacts")
    op.drop_index("ix_workspace_artifacts_thread_scope", table_name="workspace_artifacts")
    op.drop_table("workspace_artifacts")
