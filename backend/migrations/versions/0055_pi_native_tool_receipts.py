"""AGT-028 recovery A: preserve native IDs and committed Host tool facts.

Additive only. No old Run backfill, transcript, checkpoint or replay contract.
This revision contains frozen DDL and never imports application ORM models.
"""
from alembic import op
import sqlalchemy as sa

revision = "0055_pi_native_tool_receipts"
down_revision = "0054_sales_quote_identity_v2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_pi_tool_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("model_step_id", sa.String(36), nullable=False),
        sa.Column("model_kind", sa.String(16), nullable=False),
        sa.Column("native_call_id", sa.String(100), nullable=False),
        sa.Column("pi_name", sa.String(120), nullable=False),
        sa.Column("tool_key", sa.String(120), nullable=False),
        sa.Column("tool_version", sa.String(120), nullable=False),
        sa.Column("toolset_version", sa.String(120), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("tool_step_id", sa.String(36), nullable=True),
        sa.Column("tool_kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("output_ref", sa.String(200), nullable=True),
        sa.Column("error_code", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "store_id", "run_id"],
            ["agent_runs.organization_id", "agent_runs.store_id", "agent_runs.id"],
            name="fk_pi_receipt_run", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["organization_id", "store_id", "run_id", "model_step_id", "model_kind"],
            ["agent_steps.organization_id", "agent_steps.store_id", "agent_steps.run_id",
             "agent_steps.id", "agent_steps.kind"],
            name="fk_pi_receipt_model", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["organization_id", "store_id", "run_id", "tool_step_id", "tool_kind"],
            ["agent_steps.organization_id", "agent_steps.store_id", "agent_steps.run_id",
             "agent_steps.id", "agent_steps.kind"],
            name="fk_pi_receipt_tool", ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "store_id", "run_id", "native_call_id",
                            name="uq_pi_receipt_native_call"),
        sa.UniqueConstraint("organization_id", "store_id", "run_id", "tool_step_id",
                            name="uq_pi_receipt_tool_step"),
        sa.CheckConstraint("contract_version = 1 AND model_kind = 'model' AND tool_kind = 'tool'",
                           name="ck_pi_receipt_contract_kinds"),
        sa.CheckConstraint("length(native_call_id) BETWEEN 1 AND 100 AND length(pi_name) > 0 "
                           "AND length(tool_key) > 0 AND length(tool_version) > 0 "
                           "AND length(toolset_version) > 0 AND length(input_digest) = 64",
                           name="ck_pi_receipt_identity"),
        sa.CheckConstraint("(state = 'accepted' AND tool_step_id IS NULL AND output_ref IS NULL AND error_code IS NULL) "
                           "OR (state = 'dispatch_intent' AND tool_step_id IS NOT NULL AND output_ref IS NULL AND error_code IS NULL) "
                           "OR (state = 'succeeded' AND tool_step_id IS NOT NULL AND output_ref IS NOT NULL AND error_code IS NULL) "
                           "OR (state IN ('failed', 'unknown') AND output_ref IS NULL AND error_code IS NOT NULL)",
                           name="ck_pi_receipt_state"),
        sa.CheckConstraint("updated_at >= created_at", name="ck_pi_receipt_times"),
    )
    op.create_index("ix_pi_receipt_run", "agent_pi_tool_receipts",
                    ["organization_id", "store_id", "run_id", "created_at"])


def downgrade():
    # Offline SQL cannot establish emptiness. Downgrades require an exclusive
    # maintenance window; production rollback disables launch, retaining facts.
    if op.get_context().as_sql:
        raise RuntimeError("pi_receipt_downgrade_requires_verified_empty_table")
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM agent_pi_tool_receipts LIMIT 1")).first() is not None:
        raise RuntimeError("pi_receipts_require_safe_forward_recovery")
    op.drop_index("ix_pi_receipt_run", table_name="agent_pi_tool_receipts")
    op.drop_table("agent_pi_tool_receipts")
