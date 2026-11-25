"""Recoverable answer and evidence receipt (AGT-024).

Revision ID: 0014_agent_answers
Revises: 0013_knowledge_commands
"""
from alembic import op
import sqlalchemy as sa
from backend.app.modules.agents.models import UTCDateTime

revision = "0014_agent_answers"
down_revision = "0013_knowledge_commands"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_answers",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("message_id", sa.String(36), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "run_id"],
                                ["agent_runs.organization_id", "agent_runs.store_id", "agent_runs.id"],
                                name="fk_agent_answers_run_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "message_id"],
                                ["agent_messages.organization_id", "agent_messages.store_id", "agent_messages.id"],
                                name="fk_agent_answers_message_scope", ondelete="RESTRICT"),
        sa.UniqueConstraint("message_id", name="uq_agent_answers_message"),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM agent_answers LIMIT 1")).first() is not None:
        raise RuntimeError("agent answers downgrade refused: table contains data")
    op.drop_table("agent_answers")
