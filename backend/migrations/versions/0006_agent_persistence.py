"""Persist agent runs, steps, and immutable tool call metadata.

Revision ID: 0006_agent_persistence
Revises: 0005_employee_auth
"""
from alembic import op
import sqlalchemy as sa


revision = "0006_agent_persistence"
down_revision = "0005_employee_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("requester_id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=True),
        sa.Column("agent_key", sa.String(120), nullable=False),
        sa.Column("agent_version", sa.String(120), nullable=False),
        sa.Column("provider_key", sa.String(120), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("prompt_version", sa.String(120), nullable=False),
        sa.Column("toolset_version", sa.String(120), nullable=False),
        sa.Column("input_ref", sa.String(200), nullable=True),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("max_input_tokens", sa.Integer(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("max_model_calls", sa.Integer(), nullable=False),
        sa.Column("max_tool_calls", sa.Integer(), nullable=False),
        sa.Column("max_steps", sa.Integer(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_agent_runs_scope_id"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_agent_runs_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "requester_id"], ["subjects.organization_id", "subjects.id"], name="fk_agent_runs_requester_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "task_id"], ["tasks.organization_id", "tasks.store_id", "tasks.id"], name="fk_agent_runs_task_scope", ondelete="RESTRICT"),
        sa.CheckConstraint("state IN ('created', 'running', 'waiting_for_human', 'waiting_for_external', 'completed', 'failed', 'cancelled')", name="ck_agent_runs_state"),
        sa.CheckConstraint("version > 0", name="ck_agent_runs_version_positive"),
        sa.CheckConstraint("max_input_tokens > 0 AND max_output_tokens > 0 AND max_model_calls > 0 AND max_tool_calls > 0 AND max_steps > 0", name="ck_agent_runs_budgets_positive"),
        sa.CheckConstraint("deadline_at > created_at", name="ck_agent_runs_deadline_after_created"),
        sa.CheckConstraint("updated_at >= created_at", name="ck_agent_runs_updated_after_created"),
        sa.CheckConstraint("length(agent_key) > 0 AND length(agent_version) > 0 AND length(provider_key) > 0 AND length(model) > 0 AND length(prompt_version) > 0 AND length(toolset_version) > 0", name="ck_agent_runs_metadata_nonempty"),
    )
    op.create_index("ix_agent_runs_scope", "agent_runs", ["organization_id", "store_id", "created_at"])
    op.create_index("ix_agent_runs_state", "agent_runs", ["organization_id", "store_id", "state", "created_at"])

    op.create_table(
        "agent_steps",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("input_ref", sa.String(200), nullable=True),
        sa.Column("output_ref", sa.String(200), nullable=True),
        sa.Column("error_code", sa.String(120), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "run_id"], ["agent_runs.organization_id", "agent_runs.store_id", "agent_runs.id"], name="fk_agent_steps_run_scope", ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "store_id", "run_id", "sequence_no", name="uq_agent_steps_run_sequence"),
        sa.UniqueConstraint("organization_id", "store_id", "run_id", "id", "kind", name="uq_agent_steps_run_id_kind"),
        sa.CheckConstraint("kind IN ('model', 'tool', 'checkpoint')", name="ck_agent_steps_kind"),
        sa.CheckConstraint("state IN ('pending', 'running', 'succeeded', 'failed', 'unknown', 'cancelled')", name="ck_agent_steps_state"),
        sa.CheckConstraint("sequence_no > 0 AND version > 0", name="ck_agent_steps_sequence_version_positive"),
        sa.CheckConstraint("(input_tokens IS NULL OR input_tokens >= 0) AND (output_tokens IS NULL OR output_tokens >= 0)", name="ck_agent_steps_usage_nonnegative"),
        sa.CheckConstraint("kind = 'model' OR (input_tokens IS NULL AND output_tokens IS NULL)", name="ck_agent_steps_nonmodel_usage_null"),
        sa.CheckConstraint("updated_at >= created_at", name="ck_agent_steps_updated_after_created"),
    )
    op.create_index("ix_agent_steps_run", "agent_steps", ["organization_id", "store_id", "run_id", "sequence_no"])

    op.create_table(
        "agent_tool_calls",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("step_id", sa.String(36), nullable=False),
        sa.Column("step_kind", sa.String(16), server_default="tool", nullable=False),
        sa.Column("tool_key", sa.String(120), nullable=False),
        sa.Column("tool_version", sa.String(120), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "run_id", "step_id", "step_kind"], ["agent_steps.organization_id", "agent_steps.store_id", "agent_steps.run_id", "agent_steps.id", "agent_steps.kind"], name="fk_agent_tool_calls_tool_step", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "store_id", "step_id", name="uq_agent_tool_calls_step"),
        sa.UniqueConstraint("organization_id", "store_id", "run_id", "idempotency_key", name="uq_agent_tool_calls_run_idempotency"),
        sa.CheckConstraint("step_kind = 'tool'", name="ck_agent_tool_calls_step_kind_tool"),
        sa.CheckConstraint("length(tool_key) > 0 AND length(tool_version) > 0 AND length(idempotency_key) > 0", name="ck_agent_tool_calls_metadata_nonempty"),
        sa.CheckConstraint("length(input_digest) = 64", name="ck_agent_tool_calls_digest_sha256"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    # Preflight every table at this revision before the first DDL operation.
    for table in (
        "agent_tool_calls", "agent_steps", "agent_runs", "auth_sessions", "employee_accounts",
        "task_idempotency", "task_events", "tasks", "approval_idempotency", "approval_requests",
        "outbox_events", "audit_events", "permission_grants", "role_assignments", "subjects",
        "stores", "organizations",
    ):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError(f"agent persistence downgrade refused because {table} contains data; restore an isolated backup or use a forward migration")
    op.drop_table("agent_tool_calls")
    op.drop_index("ix_agent_steps_run", table_name="agent_steps")
    op.drop_table("agent_steps")
    op.drop_index("ix_agent_runs_state", table_name="agent_runs")
    op.drop_index("ix_agent_runs_scope", table_name="agent_runs")
    op.drop_table("agent_runs")
