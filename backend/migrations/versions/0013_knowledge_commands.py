"""Knowledge command receipts (CORE-005).

Revision ID: 0013_knowledge_commands
Revises: 0012_agent_context
"""
from alembic import op
import sqlalchemy as sa
from backend.app.modules.agents.models import UTCDateTime

revision = "0013_knowledge_commands"
down_revision = "0012_agent_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_commands",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "actor_id", "operation", "idempotency_key",
                            name="uq_knowledge_commands_scope_key"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                                name="fk_knowledge_commands_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "actor_id"], ["subjects.organization_id", "subjects.id"],
                                name="fk_knowledge_commands_actor_scope", ondelete="RESTRICT"),
        sa.CheckConstraint("operation IN ('submit', 'revise', 'review', 'withdraw')",
                           name="ck_knowledge_commands_operation"),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM knowledge_commands LIMIT 1")).first() is not None:
        raise RuntimeError("knowledge commands downgrade refused: table contains data")
    op.drop_table("knowledge_commands")
