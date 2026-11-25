"""Persist exact pending Agent actions for human confirmation.

Revision ID: 0008_agent_confirmation
Revises: 0007_task_policy
"""

from alembic import op
import sqlalchemy as sa

from backend.app.modules.agents.models import UTCDateTime

revision = "0008_agent_confirmation"
down_revision = "0007_task_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("uq_agent_steps_confirmation_scope", "agent_steps",
                    ["organization_id", "store_id", "run_id", "id"], unique=True)
    op.create_table(
        "agent_confirmations",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("step_id", sa.String(36), nullable=False),
        sa.Column("requester_id", sa.String(36), nullable=False),
        sa.Column("tool_key", sa.String(120), nullable=False),
        sa.Column("tool_version", sa.String(120), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("decision_reason", sa.String(500)),
        sa.Column("result_task_id", sa.String(36)),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "run_id"],
                                ["agent_runs.organization_id", "agent_runs.store_id", "agent_runs.id"],
                                name="fk_agent_confirmations_run_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "run_id", "step_id"],
                                ["agent_steps.organization_id", "agent_steps.store_id", "agent_steps.run_id", "agent_steps.id"],
                                name="fk_agent_confirmations_step_scope", ondelete="RESTRICT"),
        sa.CheckConstraint("state IN ('pending', 'executing', 'approved', 'rejected', 'expired', 'failed', 'unknown')",
                           name="ck_agent_confirmations_state"),
        sa.CheckConstraint("version > 0 AND length(input_digest) = 64", name="ck_agent_confirmations_version_digest"),
        sa.CheckConstraint("expires_at > created_at", name="ck_agent_confirmations_expiry"),
    )
    op.create_index("ix_agent_confirmations_scope_state", "agent_confirmations",
                    ["organization_id", "store_id", "state", "expires_at"])


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("agent_confirmations", "agent_tool_calls", "agent_steps", "agent_runs",
                  "task_idempotency", "task_events", "tasks", "auth_sessions", "employee_accounts",
                  "approval_idempotency", "approval_requests", "outbox_events", "audit_events",
                  "role_assignments", "permission_grants", "subjects", "stores", "organizations"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("agent confirmation downgrade refused because platform data exists; use forward recovery")
    op.drop_index("ix_agent_confirmations_scope_state", table_name="agent_confirmations")
    op.drop_table("agent_confirmations")
    op.drop_index("uq_agent_steps_confirmation_scope", table_name="agent_steps")
