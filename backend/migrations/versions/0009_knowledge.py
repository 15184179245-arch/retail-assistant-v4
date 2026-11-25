"""Knowledge entries and versions (KNW-001).

Revision ID: 0009_knowledge
Revises: 0008_agent_confirmation
"""

from alembic import op
import sqlalchemy as sa

from backend.app.modules.agents.models import UTCDateTime

revision = "0009_knowledge"
down_revision = "0008_agent_confirmation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("scope_ref", sa.String(200)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("current_revision_id", sa.String(36)),
        sa.Column("submitted_by", sa.String(36), nullable=False),
        sa.Column("submitted_at", UTCDateTime(), nullable=False),
        sa.Column("approved_by", sa.String(36)),
        sa.Column("approved_at", UTCDateTime()),
        sa.Column("decision_reason", sa.String(500)),
        sa.Column("withdrawn_by", sa.String(36)),
        sa.Column("withdrawn_at", UTCDateTime()),
        sa.Column("withdraw_reason", sa.String(500)),
        sa.Column("expires_at", UTCDateTime()),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_knowledge_entries_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "submitted_by"],
                                ["subjects.organization_id", "subjects.id"],
                                name="fk_knowledge_entries_submitter_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "approved_by"],
                                ["subjects.organization_id", "subjects.id"],
                                name="fk_knowledge_entries_approver_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "withdrawn_by"],
                                ["subjects.organization_id", "subjects.id"],
                                name="fk_knowledge_entries_withdrawer_scope", ondelete="RESTRICT"),
        sa.CheckConstraint("status IN ('pending_review', 'approved', 'rejected', 'withdrawn', 'expired')",
                           name="ck_knowledge_entries_status"),
        sa.CheckConstraint("scope_type IN ('store', 'product', 'category', 'global')",
                           name="ck_knowledge_entries_scope_type"),
        sa.CheckConstraint("version > 0", name="ck_knowledge_entries_version_positive"),
        sa.CheckConstraint("updated_at >= created_at", name="ck_knowledge_entries_updated_after_created"),
        sa.UniqueConstraint("organization_id", "store_id", "id",
                            name="uq_knowledge_entries_scope_id"),
    )
    op.create_index("ix_knowledge_entries_scope_status", "knowledge_entries",
                    ["organization_id", "store_id", "status", "expires_at"])

    op.create_table(
        "knowledge_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("entry_id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(24), nullable=False),
        sa.Column("source_ref", sa.String(500)),
        sa.Column("submitted_by", sa.String(36), nullable=False),
        sa.Column("submitted_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_knowledge_revisions_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["entry_id"], ["knowledge_entries.id"],
                                name="fk_knowledge_revisions_entry", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "submitted_by"],
                                ["subjects.organization_id", "subjects.id"],
                                name="fk_knowledge_revisions_submitter_scope", ondelete="RESTRICT"),
        sa.CheckConstraint("version > 0", name="ck_knowledge_revisions_version_positive"),
        sa.CheckConstraint("source_type IN ('manual', 'file', 'link', 'import')",
                           name="ck_knowledge_revisions_source_type"),
        sa.CheckConstraint("length(trim(title)) > 0 AND length(trim(body)) > 0",
                           name="ck_knowledge_revisions_content_nonempty"),
        sa.UniqueConstraint("entry_id", "version", name="uq_knowledge_revisions_version"),
        sa.UniqueConstraint("organization_id", "store_id", "id",
                            name="uq_knowledge_revisions_scope_id"),
    )
    op.create_index("ix_knowledge_revisions_entry", "knowledge_revisions",
                    ["entry_id", "version"])


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("knowledge_revisions", "knowledge_entries"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("knowledge downgrade refused: table contains synthetic or business data")
    op.drop_index("ix_knowledge_revisions_entry", table_name="knowledge_revisions")
    op.drop_table("knowledge_revisions")
    op.drop_index("ix_knowledge_entries_scope_status", table_name="knowledge_entries")
    op.drop_table("knowledge_entries")
