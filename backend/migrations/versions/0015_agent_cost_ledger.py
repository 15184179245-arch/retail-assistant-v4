"""Persistent cost budgets and per-call reservation ledger (AGT-019).

Revision ID: 0015_agent_cost_ledger
Revises: 0014_agent_answers

Forward-only: no default budgets, synthetic prices or business rows are
inserted. Downgrade is refused while either new table still contains data so
reserved (including unknown) cost history is never silently discarded.
"""
from alembic import op
import sqlalchemy as sa
from backend.app.modules.agents.models import UTCDateTime

revision = "0015_agent_cost_ledger"
down_revision = "0014_agent_answers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_budget_policies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("subject_id", sa.String(36), nullable=True),
        sa.Column("provider_key", sa.String(120), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("price_version", sa.String(120), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("limit_amount", sa.String(40), nullable=False),
        sa.Column("per_call_limit", sa.String(40), nullable=False),
        sa.Column("effective_from", UTCDateTime(), nullable=False),
        sa.Column("effective_until", UTCDateTime(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_agent_budget_policies_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "subject_id"],
                                ["subjects.organization_id", "subjects.id"],
                                name="fk_agent_budget_policies_subject_scope", ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "store_id", "id",
                           name="uq_agent_budget_policies_scope_id"),
        sa.CheckConstraint("scope_type IN ('employee', 'store')",
                          name="ck_agent_budget_policies_scope_type"),
        sa.CheckConstraint("(scope_type = 'employee' AND subject_id IS NOT NULL) OR "
                           "(scope_type = 'store' AND subject_id IS NULL)",
                          name="ck_agent_budget_policies_scope_subject"),
        sa.CheckConstraint("length(currency) = 3 AND currency = upper(currency)",
                          name="ck_agent_budget_policies_currency"),
        sa.CheckConstraint("length(limit_amount) BETWEEN 1 AND 40 AND length(per_call_limit) BETWEEN 1 AND 40",
                          name="ck_agent_budget_policies_limit_length"),
        sa.CheckConstraint("version > 0", name="ck_agent_budget_policies_version_positive"),
        sa.CheckConstraint("effective_until IS NULL OR effective_until > effective_from",
                          name="ck_agent_budget_policies_window"),
        sa.CheckConstraint("length(provider_key) > 0 AND length(model) > 0 AND length(price_version) > 0",
                          name="ck_agent_budget_policies_binding_nonempty"),
        sa.CheckConstraint("updated_at >= created_at",
                          name="ck_agent_budget_policies_updated_after_created"),
    )
    op.create_index("ix_agent_budget_policies_active", "agent_budget_policies",
                    ["organization_id", "store_id", "scope_type", "active", "effective_from"])
    op.create_index(
        "uq_agent_budget_policies_employee_window", "agent_budget_policies",
        ["organization_id", "store_id", "subject_id", "effective_from"], unique=True,
        sqlite_where=sa.text("scope_type = 'employee'"),
        postgresql_where=sa.text("scope_type = 'employee'"))
    op.create_index(
        "uq_agent_budget_policies_store_window", "agent_budget_policies",
        ["organization_id", "store_id", "effective_from"], unique=True,
        sqlite_where=sa.text("scope_type = 'store'"),
        postgresql_where=sa.text("scope_type = 'store'"))
    op.create_index("ix_agent_budget_policies_subject", "agent_budget_policies",
                    ["organization_id", "store_id", "subject_id", "active"])

    op.create_table(
        "agent_cost_ledger",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("step_id", sa.String(36), nullable=False),
        sa.Column("call_key", sa.String(200), nullable=False),
        sa.Column("requester_id", sa.String(36), nullable=False),
        sa.Column("provider_key", sa.String(120), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("price_version", sa.String(120), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("reserved_amount", sa.String(40), nullable=False),
        sa.Column("estimated_amount", sa.String(40), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reserved_usage", sa.JSON(), nullable=False),
        sa.Column("usage_snapshot", sa.JSON(), nullable=True),
        sa.Column("price_snapshot", sa.JSON(), nullable=False),
        sa.Column("employee_budget_id", sa.String(36), nullable=True),
        sa.Column("store_budget_id", sa.String(36), nullable=True),
        sa.Column("error_code", sa.String(120), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("settled_at", UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "run_id"],
                                ["agent_runs.organization_id", "agent_runs.store_id", "agent_runs.id"],
                                name="fk_agent_cost_ledger_run_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "run_id", "step_id"],
                                ["agent_steps.organization_id", "agent_steps.store_id",
                                 "agent_steps.run_id", "agent_steps.id"],
                                name="fk_agent_cost_ledger_step_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "requester_id"],
                                ["subjects.organization_id", "subjects.id"],
                                name="fk_agent_cost_ledger_requester_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "employee_budget_id"],
                                ["agent_budget_policies.organization_id",
                                 "agent_budget_policies.store_id", "agent_budget_policies.id"],
                                name="fk_agent_cost_ledger_employee_budget", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "store_budget_id"],
                                ["agent_budget_policies.organization_id",
                                 "agent_budget_policies.store_id", "agent_budget_policies.id"],
                                name="fk_agent_cost_ledger_store_budget", ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "store_id", "run_id", "step_id", "call_key",
                           name="uq_agent_cost_ledger_call"),
        sa.CheckConstraint("status IN ('reserved', 'settled', 'unknown', 'released', "
                           "'reconciliation_required')", name="ck_agent_cost_ledger_status"),
        sa.CheckConstraint("length(currency) = 3 AND currency = upper(currency)",
                          name="ck_agent_cost_ledger_currency"),
        sa.CheckConstraint("length(reserved_amount) BETWEEN 1 AND 40",
                          name="ck_agent_cost_ledger_reserved_amount"),
        sa.CheckConstraint("estimated_amount IS NULL OR length(estimated_amount) BETWEEN 1 AND 40",
                          name="ck_agent_cost_ledger_estimated_amount"),
        sa.CheckConstraint("status NOT IN ('settled', 'reconciliation_required') "
                           "OR estimated_amount IS NOT NULL",
                          name="ck_agent_cost_ledger_terminal_amount"),
        sa.CheckConstraint("length(call_key) > 0 AND length(provider_key) > 0 "
                           "AND length(model) > 0 AND length(price_version) > 0",
                          name="ck_agent_cost_ledger_identity_nonempty"),
        sa.CheckConstraint("updated_at >= created_at",
                          name="ck_agent_cost_ledger_updated_after_created"),
    )
    op.create_index("ix_agent_cost_ledger_scope_time", "agent_cost_ledger",
                    ["organization_id", "store_id", "created_at", "id"])
    op.create_index("ix_agent_cost_ledger_requester_time", "agent_cost_ledger",
                    ["organization_id", "store_id", "requester_id", "created_at", "id"])
    op.create_index("ix_agent_cost_ledger_run", "agent_cost_ledger",
                    ["organization_id", "store_id", "run_id"])
    op.create_index("ix_agent_cost_ledger_status", "agent_cost_ledger",
                    ["organization_id", "store_id", "status"])


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("agent_cost_ledger", "agent_budget_policies"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("agent cost ledger downgrade refused: table contains data")
    op.drop_table("agent_cost_ledger")
    op.drop_table("agent_budget_policies")
