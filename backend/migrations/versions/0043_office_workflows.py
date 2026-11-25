"""Bounded manual office workflow and immutable command receipts.

No task assignment, approval, Agent execution or external business write is
performed here. All additions are structural; current authority is checked by
the later service, never inferred from these foreign keys.
"""
from alembic import op
import sqlalchemy as sa

revision = "0043_office_workflows"
down_revision = "0042_erp_sandbox_dispatch"
branch_labels = None
depends_on = None


def _scope():
    return [sa.Column("organization_id", sa.String(36), nullable=False),
            sa.Column("store_id", sa.String(36), nullable=False)]


def _scoped_fk(local, table, remote, name):
    return sa.ForeignKeyConstraint(
        ["organization_id", "store_id", local],
        [f"{table}.organization_id", f"{table}.store_id", f"{table}.{remote}"],
        name=name, ondelete="RESTRICT")


def _receipt_guards(bind):
    if bind.dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            bind.exec_driver_sql(
                f"CREATE TRIGGER trg_office_workflow_receipts_{operation.lower()}_deny "
                f"BEFORE {operation} ON office_workflow_receipts "
                "BEGIN SELECT RAISE(ABORT,'office workflow receipts are immutable'); END")
    elif bind.dialect.name == "postgresql":
        bind.exec_driver_sql(
            "CREATE FUNCTION deny_office_workflow_receipts_mutation() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "RAISE EXCEPTION 'office workflow receipts are immutable'; END; $$")
        bind.exec_driver_sql(
            "CREATE TRIGGER trg_office_workflow_receipts_deny BEFORE UPDATE OR DELETE "
            "ON office_workflow_receipts FOR EACH ROW "
            "EXECUTE FUNCTION deny_office_workflow_receipts_mutation()")
    else:
        raise RuntimeError("office workflow receipts require SQLite or PostgreSQL immutable guards")


def upgrade():
    op.create_table("office_workflows",
        sa.Column("id", sa.String(36), primary_key=True), *_scope(),
        sa.Column("template_key", sa.String(40), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("execution_mode", sa.String(24), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("task_context_digest", sa.String(64), nullable=False),
        sa.Column("source_evidence_id", sa.String(36), nullable=False),
        sa.Column("source_evidence_digest", sa.String(64), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("rule_id", sa.String(36)),
        sa.Column("rule_digest", sa.String(64)),
        sa.Column("origin_run_id", sa.String(36)),
        sa.Column("create_key", sa.String(200), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("next_step_no", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_office_workflow_scope"),
        sa.UniqueConstraint("organization_id", "store_id", "task_id", name="uq_office_workflow_task"),
        sa.UniqueConstraint("organization_id", "store_id", "create_key", name="uq_office_workflow_create_key"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
            ["stores.organization_id", "stores.id"], name="fk_office_workflow_store", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "created_by"],
            ["subjects.organization_id", "subjects.id"], name="fk_office_workflow_creator", ondelete="RESTRICT"),
        _scoped_fk("task_id", "tasks", "id", "fk_office_workflow_task"),
        _scoped_fk("source_evidence_id", "office_evidence", "id", "fk_office_workflow_evidence"),
        _scoped_fk("rule_id", "office_rule_versions", "id", "fk_office_workflow_rule"),
        _scoped_fk("origin_run_id", "agent_runs", "id", "fk_office_workflow_origin_run"),
        sa.CheckConstraint("template_key='material_review.v1'", name="ck_office_workflow_template_key"),
        sa.CheckConstraint("template_version=1", name="ck_office_workflow_template_version"),
        sa.CheckConstraint("execution_mode='manual_task'", name="ck_office_workflow_execution_mode"),
        sa.CheckConstraint("length(task_context_digest)=64 AND length(source_evidence_digest)=64 AND length(input_digest)=64", name="ck_office_workflow_digests"),
        sa.CheckConstraint("(rule_id IS NULL AND rule_digest IS NULL) OR (rule_id IS NOT NULL AND rule_digest IS NOT NULL AND length(rule_digest)=64)", name="ck_office_workflow_rule_pair"),
        sa.CheckConstraint("length(create_key) BETWEEN 1 AND 200", name="ck_office_workflow_create_key_length"),
        sa.CheckConstraint("state IN ('active','paused','needs_human','failed','closed')", name="ck_office_workflow_state"),
        sa.CheckConstraint("version>0", name="ck_office_workflow_version_positive"),
        sa.CheckConstraint("next_step_no BETWEEN 1 AND 8", name="ck_office_workflow_next_step"),
        sa.CheckConstraint("updated_at>=created_at", name="ck_office_workflow_times"),
        sa.CheckConstraint("(state='closed' AND next_step_no=8 AND closed_at IS NOT NULL) OR (state<>'closed' AND next_step_no<=7 AND closed_at IS NULL)", name="ck_office_workflow_closed"))
    op.create_index("ix_office_workflow_scope_state", "office_workflows", ["organization_id", "store_id", "state", "updated_at", "id"])
    op.create_table("office_workflow_steps", *_scope(),
        sa.Column("workflow_id", sa.String(36), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("responsible_id", sa.String(36), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("input_digest", sa.String(64)),
        sa.Column("output_digest", sa.String(64)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("organization_id", "store_id", "workflow_id", "step_no"),
        _scoped_fk("workflow_id", "office_workflows", "id", "fk_office_workflow_step_parent"),
        sa.ForeignKeyConstraint(["organization_id", "responsible_id"], ["subjects.organization_id", "subjects.id"], name="fk_office_workflow_step_responsible", ondelete="RESTRICT"),
        sa.CheckConstraint("step_no BETWEEN 1 AND 7", name="ck_office_workflow_step_no"),
        sa.CheckConstraint("(step_no=1 AND kind='intake') OR (step_no=2 AND kind='extraction') OR (step_no=3 AND kind='review') OR (step_no=4 AND kind='approval') OR (step_no=5 AND kind='execution') OR (step_no=6 AND kind='verification') OR (step_no=7 AND kind='closure')", name="ck_office_workflow_step_kind_map"),
        sa.CheckConstraint("state IN ('pending','completed','failed','unknown')", name="ck_office_workflow_step_state"),
        sa.CheckConstraint("version>0", name="ck_office_workflow_step_version_positive"),
        sa.CheckConstraint("(input_digest IS NULL OR length(input_digest)=64) AND (output_digest IS NULL OR length(output_digest)=64)", name="ck_office_workflow_step_digests"),
        sa.CheckConstraint("(state='completed' AND input_digest IS NOT NULL AND output_digest IS NOT NULL AND completed_at IS NOT NULL) OR (state<>'completed' AND completed_at IS NULL)", name="ck_office_workflow_step_completion"))
    op.create_index("ix_office_workflow_step_responsible", "office_workflow_steps", ["organization_id", "store_id", "responsible_id", "state"])
    op.create_table("office_workflow_receipts",
        sa.Column("id", sa.String(36), primary_key=True), *_scope(),
        sa.Column("workflow_id", sa.String(36), nullable=False),
        sa.Column("step_no", sa.Integer()),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("workflow_version", sa.Integer(), nullable=False),
        sa.Column("request_key", sa.String(200), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("output_digest", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("references", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_office_workflow_receipt_scope"),
        sa.UniqueConstraint("organization_id", "store_id", "workflow_id", "workflow_version", name="uq_office_workflow_receipt_version"),
        sa.UniqueConstraint("organization_id", "store_id", "request_key", name="uq_office_workflow_receipt_request"),
        _scoped_fk("workflow_id", "office_workflows", "id", "fk_office_workflow_receipt_parent"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "workflow_id", "step_no"],
            ["office_workflow_steps.organization_id", "office_workflow_steps.store_id", "office_workflow_steps.workflow_id", "office_workflow_steps.step_no"], name="fk_office_workflow_receipt_step", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "actor_id"], ["subjects.organization_id", "subjects.id"], name="fk_office_workflow_receipt_actor", ondelete="RESTRICT"),
        sa.CheckConstraint("operation IN ('create','advance','pause','resume','fail','mark_unknown')", name="ck_office_workflow_receipt_operation"),
        sa.CheckConstraint("(operation='advance' AND step_no IS NOT NULL AND step_no BETWEEN 1 AND 7) OR (operation<>'advance' AND step_no IS NULL)", name="ck_office_workflow_receipt_step_binding"),
        sa.CheckConstraint("workflow_version>0", name="ck_office_workflow_receipt_version_positive"),
        sa.CheckConstraint("length(request_key) BETWEEN 1 AND 200", name="ck_office_workflow_receipt_request_key_length"),
        sa.CheckConstraint("length(request_digest)=64 AND length(input_digest)=64 AND length(output_digest)=64", name="ck_office_workflow_receipt_digests"))
    op.create_index("ix_office_workflow_receipt_timeline", "office_workflow_receipts", ["organization_id", "store_id", "workflow_id", "created_at", "id"])
    _receipt_guards(op.get_bind())


def downgrade():
    bind = op.get_bind()
    # Validate the immediate predecessor's guards before dropping any new table.
    # A later refusal must not silently remove even this revision's empty tables.
    for table in ("office_workflow_receipts", "office_workflow_steps", "office_workflows",
                  "erp_sandbox_observations", "erp_sandbox_lines", "erp_sandbox_batches", "scope_security_fences"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("0043 downgrade refused while workflow, sandbox or security-fence data exists; forward-fix")
    op.drop_table("office_workflow_receipts")
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql("DROP FUNCTION deny_office_workflow_receipts_mutation()")
    op.drop_table("office_workflow_steps")
    op.drop_table("office_workflows")
