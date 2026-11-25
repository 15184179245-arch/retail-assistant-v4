"""AGT-021: durable scoped Agent worker control; no legacy Run rewrites.

Revision ID: 0030_agent_worker_control
Revises: 0029_skill_release
"""
from alembic import op
import sqlalchemy as sa

from backend.app.modules.agents.models import UTCDateTime

revision = "0030_agent_worker_control"
down_revision = "0029_skill_release"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_run_executions",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False, server_default="worker_v2"),
        sa.Column("phase", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("owner_machine_id", sa.String(36)),
        sa.Column("lease_until", UTCDateTime()),
        sa.Column("account_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("account_version", sa.Integer(), nullable=False),
        sa.Column("authorization_expires_at", UTCDateTime(), nullable=False),
        sa.Column("origin_message_id", sa.String(36), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("frozen_context", sa.JSON(), nullable=False),
        sa.Column("cancel_requested_at", UTCDateTime()),
        sa.Column("cancel_requested_by", sa.String(36)),
        sa.Column("cancel_resolution", sa.String(16), nullable=False, server_default="not_requested"),
        sa.Column("effect_status", sa.String(24), nullable=False, server_default="none"),
        sa.Column("dispatch_state", sa.String(16), nullable=False, server_default="not_dispatched"),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "run_id"],
            ["agent_runs.organization_id", "agent_runs.store_id", "agent_runs.id"],
            name="fk_agent_execution_run_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "owner_machine_id"],
            ["machine_identities.organization_id", "machine_identities.store_id", "machine_identities.id"],
            name="fk_agent_execution_machine_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["session_id"], ["auth_sessions.id"],
            name="fk_agent_execution_session", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["account_id"], ["employee_accounts.id"],
            name="fk_agent_execution_account", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "origin_message_id"],
            ["agent_messages.organization_id", "agent_messages.store_id", "agent_messages.id"],
            name="fk_agent_execution_origin_scope", ondelete="RESTRICT"),
        sa.CheckConstraint("mode = 'worker_v2'", name="ck_agent_execution_mode"),
        sa.CheckConstraint("phase IN ('queued','claimed','unknown','finished','cancelled')",
            name="ck_agent_execution_phase"),
        sa.CheckConstraint("version > 0 AND epoch >= 0 AND account_version > 0",
            name="ck_agent_execution_versions"),
        sa.CheckConstraint("(phase = 'claimed' AND owner_machine_id IS NOT NULL AND lease_until IS NOT NULL) "
            "OR (phase != 'claimed' AND owner_machine_id IS NULL AND lease_until IS NULL)",
            name="ck_agent_execution_lease_pair"),
        sa.CheckConstraint("cancel_resolution IN ('not_requested','requested','cancelled','too_late','unknown')",
            name="ck_agent_execution_cancel_resolution"),
        sa.CheckConstraint("effect_status IN ('none','local_task_committed','external_unknown')",
            name="ck_agent_execution_effect_status"),
        sa.CheckConstraint("dispatch_state IN ('not_dispatched','dispatched','unknown','settled')",
            name="ck_agent_execution_dispatch"),
        sa.CheckConstraint("length(input_digest) = 64", name="ck_agent_execution_digest"),
    )
    op.create_index("ix_agent_execution_claim", "agent_run_executions",
        ["organization_id", "store_id", "phase", "lease_until", "created_at"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM agent_run_executions LIMIT 1")).first():
        raise RuntimeError("0030 agent execution downgrade refused with durable records; forward-fix")
    op.drop_index("ix_agent_execution_claim", table_name="agent_run_executions")
    op.drop_table("agent_run_executions")
