"""Scoped exception ownership and immutable human handover receipts.

Tracking only: no source-domain state, Task assignment or external write.
The schema is explicit and independent of future application ORM definitions.
"""
from alembic import op
import sqlalchemy as sa

revision = "0044_office_exceptions"
down_revision = "0043_office_workflows"
branch_labels = None
depends_on = None

SOURCES = (
    ("receiving_discrepancy", "receiving_discrepancy_id", "receiving_discrepancies",
     "'shortage','overage','wrong_item','identity_ambiguous','price_change'"),
    ("erp_sandbox", "erp_sandbox_batch_id", "erp_sandbox_batches", "'partial','unknown_write'"),
    ("erp_intent", "erp_intent_id", "erp_command_intents", "'unknown_write'"),
    ("sales_handoff", "sales_handoff_id", "sales_handoffs", "'unknown_write'"),
    ("workflow", "workflow_id", "office_workflows", "'workflow_attention'"),
    ("task", "task_id", "tasks", "'overdue'"),
)


def _scope():
    return [sa.Column("organization_id", sa.String(36), nullable=False),
            sa.Column("store_id", sa.String(36), nullable=False)]


def _scoped_fk(local, table, remote, name):
    return sa.ForeignKeyConstraint(
        ["organization_id", "store_id", local],
        [f"{table}.organization_id", f"{table}.store_id", f"{table}.{remote}"],
        name=name, ondelete="RESTRICT")


def _subject_fk(local, name):
    return sa.ForeignKeyConstraint(["organization_id", local],
        ["subjects.organization_id", "subjects.id"], name=name, ondelete="RESTRICT")


def _receipt_guards(bind):
    if bind.dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            bind.exec_driver_sql(
                f"CREATE TRIGGER trg_office_exception_receipts_{operation.lower()}_deny "
                f"BEFORE {operation} ON office_exception_receipts "
                "BEGIN SELECT RAISE(ABORT,'office exception receipts are immutable'); END")
    elif bind.dialect.name == "postgresql":
        bind.exec_driver_sql(
            "CREATE FUNCTION deny_office_exception_receipts_mutation() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "RAISE EXCEPTION 'office exception receipts are immutable'; END; $$")
        bind.exec_driver_sql(
            "CREATE TRIGGER trg_office_exception_receipts_deny BEFORE UPDATE OR DELETE "
            "ON office_exception_receipts FOR EACH ROW "
            "EXECUTE FUNCTION deny_office_exception_receipts_mutation()")
    else:
        raise RuntimeError("office exception receipts require SQLite or PostgreSQL guards")


def upgrade():
    op.create_table("office_exception_cases",
        sa.Column("id", sa.String(36), primary_key=True), *_scope(),
        sa.Column("source_type", sa.String(24), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        *[sa.Column(column, sa.String(36), nullable=True) for _, column, _, _ in SOURCES],
        sa.Column("initial_source_digest", sa.String(64), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.String(36), nullable=True),
        sa.Column("pending_owner_id", sa.String(36), nullable=True),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("create_key", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_digest", sa.String(64), nullable=True),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_office_exception_case_scope"),
        sa.UniqueConstraint("organization_id", "store_id", "source_type", "source_id", "kind",
                            name="uq_office_exception_case_source"),
        sa.UniqueConstraint("organization_id", "store_id", "create_key", name="uq_office_exception_case_create_key"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                                name="fk_office_exception_case_store", ondelete="RESTRICT"),
        *[_scoped_fk(column, table, "id", f"fk_office_exception_case_{source}")
          for source, column, table, _ in SOURCES],
        *[_subject_fk(column, "fk_office_exception_case_" + suffix)
          for column, suffix in (("owner_id", "owner"), ("pending_owner_id", "pending_owner"), ("created_by", "creator"))],
        sa.CheckConstraint('length(create_key) BETWEEN 1 AND 200',
                           name='ck_office_exception_case_create_key_length'),
        sa.CheckConstraint('length(initial_source_digest) = 64 AND (resolution_digest IS NULL OR length(resolution_digest) = 64)',
                           name='ck_office_exception_case_digests'),
        sa.CheckConstraint("(source_type = 'receiving_discrepancy' AND kind IN ('shortage', 'overage', 'wrong_item', 'identity_ambiguous', 'price_change')) OR (source_type = 'erp_sandbox' AND kind IN ('partial', 'unknown_write')) OR (source_type = 'erp_intent' AND kind = 'unknown_write') OR (source_type = 'sales_handoff' AND kind = 'unknown_write') OR (source_type = 'workflow' AND kind = 'workflow_attention') OR (source_type = 'task' AND kind = 'overdue')",
                           name='ck_office_exception_case_kind_map'),
        sa.CheckConstraint("(source_type = 'receiving_discrepancy' AND receiving_discrepancy_id IS NOT NULL AND source_id = receiving_discrepancy_id AND erp_sandbox_batch_id IS NULL AND erp_intent_id IS NULL AND sales_handoff_id IS NULL AND workflow_id IS NULL AND task_id IS NULL) OR (source_type = 'erp_sandbox' AND erp_sandbox_batch_id IS NOT NULL AND source_id = erp_sandbox_batch_id AND receiving_discrepancy_id IS NULL AND erp_intent_id IS NULL AND sales_handoff_id IS NULL AND workflow_id IS NULL AND task_id IS NULL) OR (source_type = 'erp_intent' AND erp_intent_id IS NOT NULL AND source_id = erp_intent_id AND receiving_discrepancy_id IS NULL AND erp_sandbox_batch_id IS NULL AND sales_handoff_id IS NULL AND workflow_id IS NULL AND task_id IS NULL) OR (source_type = 'sales_handoff' AND sales_handoff_id IS NOT NULL AND source_id = sales_handoff_id AND receiving_discrepancy_id IS NULL AND erp_sandbox_batch_id IS NULL AND erp_intent_id IS NULL AND workflow_id IS NULL AND task_id IS NULL) OR (source_type = 'workflow' AND workflow_id IS NOT NULL AND source_id = workflow_id AND receiving_discrepancy_id IS NULL AND erp_sandbox_batch_id IS NULL AND erp_intent_id IS NULL AND sales_handoff_id IS NULL AND task_id IS NULL) OR (source_type = 'task' AND task_id IS NOT NULL AND source_id = task_id AND receiving_discrepancy_id IS NULL AND erp_sandbox_batch_id IS NULL AND erp_intent_id IS NULL AND sales_handoff_id IS NULL AND workflow_id IS NULL)",
                           name='ck_office_exception_case_selector'),
        sa.CheckConstraint('source_version IS NULL OR source_version > 0',
                           name='ck_office_exception_case_source_version'),
        sa.CheckConstraint("state IN ('open', 'owned', 'transfer_pending', 'resolved')",
                           name='ck_office_exception_case_state'),
        sa.CheckConstraint("(state = 'open' AND owner_id IS NULL AND pending_owner_id IS NULL AND resolved_at IS NULL AND resolution_digest IS NULL) OR (state = 'owned' AND owner_id IS NOT NULL AND pending_owner_id IS NULL AND resolved_at IS NULL AND resolution_digest IS NULL) OR (state = 'transfer_pending' AND owner_id IS NOT NULL AND pending_owner_id IS NOT NULL AND pending_owner_id <> owner_id AND resolved_at IS NULL AND resolution_digest IS NULL) OR (state = 'resolved' AND owner_id IS NOT NULL AND pending_owner_id IS NULL AND resolved_at IS NOT NULL AND resolution_digest IS NOT NULL AND resolved_at >= created_at)",
                           name='ck_office_exception_case_state_shape'),
        sa.CheckConstraint('updated_at >= created_at',
                           name='ck_office_exception_case_times'),
        sa.CheckConstraint('version > 0',
                           name='ck_office_exception_case_version_positive'))
    op.create_index("ix_office_exception_case_queue", "office_exception_cases",
                    ["organization_id", "store_id", "state", "created_at", "id"])
    op.create_index("ix_office_exception_case_owner", "office_exception_cases",
                    ["organization_id", "store_id", "owner_id", "state"])
    op.create_table("office_exception_receipts",
        sa.Column("id", sa.String(36), primary_key=True), *_scope(),
        sa.Column("case_id", sa.String(36), nullable=False),
        sa.Column("operation", sa.String(24), nullable=False),
        sa.Column("case_version", sa.Integer(), nullable=False),
        sa.Column("request_key", sa.String(200), nullable=False),
        *[sa.Column(column, sa.String(64), nullable=False)
          for column in ("request_digest", "source_digest", "reason_digest")],
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("from_owner_id", sa.String(36), nullable=True),
        sa.Column("to_owner_id", sa.String(36), nullable=True),
        sa.Column("resolution_digest", sa.String(64), nullable=True),
        sa.Column("references", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_office_exception_receipt_scope"),
        sa.UniqueConstraint("organization_id", "store_id", "case_id", "case_version",
                            name="uq_office_exception_receipt_version"),
        sa.UniqueConstraint("organization_id", "store_id", "request_key", name="uq_office_exception_receipt_request"),
        _scoped_fk("case_id", "office_exception_cases", "id", "fk_office_exception_receipt_parent"),
        *[_subject_fk(column, "fk_office_exception_receipt_" + suffix)
          for column, suffix in (("actor_id", "actor"), ("from_owner_id", "from_owner"), ("to_owner_id", "to_owner"))],
        sa.CheckConstraint('length(request_digest) = 64 AND length(source_digest) = 64 AND length(reason_digest) = 64 AND (resolution_digest IS NULL OR length(resolution_digest) = 64)',
                           name='ck_office_exception_receipt_digests'),
        sa.CheckConstraint("operation IN ('create', 'claim', 'takeover', 'propose_transfer', 'accept_transfer', 'cancel_transfer', 'resolve', 'reopen')",
                           name='ck_office_exception_receipt_operation'),
        sa.CheckConstraint('length(request_key) BETWEEN 1 AND 200',
                           name='ck_office_exception_receipt_request_key_length'),
        sa.CheckConstraint("(operation = 'resolve' AND resolution_digest IS NOT NULL) OR (operation <> 'resolve' AND resolution_digest IS NULL)",
                           name='ck_office_exception_receipt_resolution'),
        sa.CheckConstraint('case_version > 0',
                           name='ck_office_exception_receipt_version_positive'))
    op.create_index("ix_office_exception_receipt_timeline", "office_exception_receipts",
                    ["organization_id", "store_id", "case_id", "case_version"])
    _receipt_guards(op.get_bind())


def downgrade():
    bind = op.get_bind()
    # Refuse before ANY DDL, including predecessor refusal conditions. Dropping
    # our empty tables and only then finding protected old rows is not atomic.
    protected = ("office_exception_cases", "office_exception_receipts",
                 "office_workflows", "office_workflow_steps", "office_workflow_receipts",
                 "erp_sandbox_batches", "erp_sandbox_lines", "erp_sandbox_observations",
                 "scope_security_fences")
    for table in protected:
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError(f"refuse downgrade 0044: {table} contains protected data; keep schema and roll forward")
    op.drop_table("office_exception_receipts")
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql("DROP FUNCTION deny_office_exception_receipts_mutation()")
    op.drop_table("office_exception_cases")
