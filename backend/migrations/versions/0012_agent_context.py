"""Personal agent conversations and controlled summaries (AGT-009).

Revision ID: 0012_agent_context
Revises: 0011_search_alias
"""

from alembic import op
import sqlalchemy as sa

from backend.app.modules.agents.models import UTCDateTime

revision = "0012_agent_context"
down_revision = "0011_search_alias"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("subject_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("summary_text", sa.String(2000)),
        sa.Column("summary_source", sa.String(120)),
        sa.Column("summary_version", sa.Integer(), nullable=False),
        sa.Column("summary_updated_at", UTCDateTime()),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_agent_conversations_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "subject_id"],
                                ["subjects.organization_id", "subjects.id"],
                                name="fk_agent_conversations_subject_scope", ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "store_id", "id",
                            name="uq_agent_conversations_scope_id"),
        sa.CheckConstraint("status IN ('active', 'closed')", name="ck_agent_conversations_status"),
        sa.CheckConstraint("summary_version >= 0 AND version > 0",
                           name="ck_agent_conversations_versions"),
    )
    op.create_index("ix_agent_conversations_subject", "agent_conversations",
                    ["organization_id", "store_id", "subject_id", "status"])
    op.create_table(
        "agent_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.String(4000), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("source_ref", sa.String(200)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "conversation_id"],
                                ["agent_conversations.organization_id",
                                 "agent_conversations.store_id", "agent_conversations.id"],
                                name="fk_agent_messages_conversation_scope", ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_agent_messages_scope_id"),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_agent_messages_role"),
        sa.CheckConstraint("state IN ('active', 'revoked')", name="ck_agent_messages_state"),
        sa.CheckConstraint("version > 0 AND length(content) > 0",
                           name="ck_agent_messages_content"),
    )
    op.create_index("ix_agent_messages_conversation", "agent_messages",
                    ["organization_id", "store_id", "conversation_id", "created_at"])


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("agent_messages", "agent_conversations"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("agent context downgrade refused: table contains data")
    op.drop_index("ix_agent_messages_conversation", table_name="agent_messages")
    op.drop_table("agent_messages")
    op.drop_index("ix_agent_conversations_subject", table_name="agent_conversations")
    op.drop_table("agent_conversations")
