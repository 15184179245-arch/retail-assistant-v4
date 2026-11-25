"""PUR-002: five receiving tables, after actual unique head 0031.

Revision ID: 0032_receiving_batches
Revises: 0031_skill_run_binding
"""
from alembic import op
import sqlalchemy as sa
from backend.app.modules.agents.models import UTCDateTime

revision = "0032_receiving_batches"
down_revision = "0031_skill_run_binding"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("receiving_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("intent_id", sa.String(36)),
        sa.Column("expected_intent_version", sa.Integer()),
        sa.Column("delivery_note_sha256", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("confirmed_by", sa.String(36)),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("confirmed_at", UTCDateTime()),
        sa.UniqueConstraint("organization_id","store_id","id",name="uq_receiving_batches_scope"),
        sa.UniqueConstraint("organization_id","store_id","delivery_note_sha256",name="uq_receiving_note_once"),
        sa.ForeignKeyConstraint(["organization_id","store_id"],["stores.organization_id","stores.id"],name="fk_receiving_batches_store",ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id","store_id","intent_id"],["purchase_intents.organization_id","purchase_intents.store_id","purchase_intents.id"],name="fk_receiving_batch_intent",ondelete="RESTRICT"),
        sa.CheckConstraint("(intent_id IS NULL AND expected_intent_version IS NULL) OR (intent_id IS NOT NULL AND expected_intent_version > 0)",name="ck_receiving_batch_intent_version"),
        sa.CheckConstraint("status IN ('observed','reviewed') AND version > 0",name="ck_receiving_batch_state"),
        sa.CheckConstraint("length(delivery_note_sha256)=64",name="ck_receiving_batch_evidence"),
        sa.CheckConstraint("(status='observed' AND confirmed_by IS NULL AND confirmed_at IS NULL) OR (status='reviewed' AND confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",name="ck_receiving_batch_confirmation"))
    op.create_index("ix_receiving_batches_scope_time","receiving_batches",["organization_id","store_id","created_at"])
    op.create_table("receiving_lines",
        sa.Column("id",sa.String(36),primary_key=True),
        sa.Column("organization_id",sa.String(36),nullable=False),
        sa.Column("store_id",sa.String(36),nullable=False),
        sa.Column("batch_id",sa.String(36),nullable=False),
        sa.Column("line_key",sa.String(80),nullable=False),
        sa.Column("intent_line_id",sa.String(36)),
        sa.Column("product_id",sa.String(36)),
        sa.Column("product_version",sa.Integer()),
        sa.Column("unknown_product_label",sa.String(200)),
        sa.Column("observed_quantity",sa.String(40),nullable=False),
        sa.Column("unit",sa.String(16),nullable=False),
        sa.Column("final_for_intent_line",sa.Boolean(),nullable=False),
        sa.Column("evidence_sha256",sa.String(64),nullable=False),
        sa.Column("quoted_price",sa.String(40)),
        sa.Column("delivered_price",sa.String(40)),
        sa.Column("price_evidence_sha256",sa.String(64)),
        sa.Column("observed_by",sa.String(36),nullable=False),
        sa.Column("observed_at",UTCDateTime(),nullable=False),
        sa.UniqueConstraint("organization_id","store_id","id",name="uq_receiving_lines_scope"),
        sa.UniqueConstraint("organization_id","store_id","batch_id","line_key",name="uq_receiving_batch_line_key"),
        sa.ForeignKeyConstraint(["organization_id","store_id","batch_id"],["receiving_batches.organization_id","receiving_batches.store_id","receiving_batches.id"],name="fk_receiving_line_batch",ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id","store_id","intent_line_id"],["purchase_intent_lines.organization_id","purchase_intent_lines.store_id","purchase_intent_lines.id"],name="fk_receiving_line_intent",ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id","store_id","product_id"],["catalog_products.organization_id","catalog_products.store_id","catalog_products.id"],name="fk_receiving_line_product",ondelete="RESTRICT"),
        sa.CheckConstraint("(product_id IS NULL AND product_version IS NULL AND unknown_product_label IS NOT NULL) OR (product_id IS NOT NULL AND product_version > 0 AND unknown_product_label IS NULL)",name="ck_receiving_line_identity"),
        sa.CheckConstraint("length(evidence_sha256)=64",name="ck_receiving_line_sha"),
        sa.CheckConstraint("(quoted_price IS NULL AND delivered_price IS NULL AND price_evidence_sha256 IS NULL) OR (quoted_price IS NOT NULL AND delivered_price IS NOT NULL AND length(price_evidence_sha256)=64)",name="ck_receiving_line_price_proof"))
    op.create_table("receiving_discrepancies",
        sa.Column("id",sa.String(36),primary_key=True),
        sa.Column("organization_id",sa.String(36),nullable=False),
        sa.Column("store_id",sa.String(36),nullable=False),
        sa.Column("batch_id",sa.String(36),nullable=False),
        sa.Column("line_id",sa.String(36),nullable=False),
        sa.Column("kind",sa.String(24),nullable=False),
        sa.Column("detail",sa.JSON(),nullable=False),
        sa.Column("created_at",UTCDateTime(),nullable=False),
        sa.UniqueConstraint("organization_id","store_id","id",name="uq_receiving_discrepancies_scope"),
        sa.UniqueConstraint("organization_id","store_id","batch_id","line_id","kind",name="uq_receiving_discrepancy_once"),
        sa.ForeignKeyConstraint(["organization_id","store_id","batch_id"],["receiving_batches.organization_id","receiving_batches.store_id","receiving_batches.id"],name="fk_receiving_discrepancy_batch",ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id","store_id","line_id"],["receiving_lines.organization_id","receiving_lines.store_id","receiving_lines.id"],name="fk_receiving_discrepancy_line",ondelete="RESTRICT"),
        sa.CheckConstraint("kind IN ('shortage','overage','wrong_item','price_change','identity_unknown')",name="ck_receiving_discrepancy_kind"))
    op.create_table("receiving_decisions",
        sa.Column("id",sa.String(36),primary_key=True),
        sa.Column("organization_id",sa.String(36),nullable=False),
        sa.Column("store_id",sa.String(36),nullable=False),
        sa.Column("batch_id",sa.String(36),nullable=False),
        sa.Column("discrepancy_id",sa.String(36),nullable=False),
        sa.Column("batch_version",sa.Integer(),nullable=False),
        sa.Column("reviewer_id",sa.String(36),nullable=False),
        sa.Column("responsible_id",sa.String(36),nullable=False),
        sa.Column("decision",sa.String(32),nullable=False),
        sa.Column("reason",sa.String(1000),nullable=False),
        sa.Column("evidence_sha256",sa.String(64),nullable=False),
        sa.Column("created_at",UTCDateTime(),nullable=False),
        sa.UniqueConstraint("organization_id","store_id","batch_id","batch_version",name="uq_receiving_decision_cas"),
        sa.ForeignKeyConstraint(["organization_id","store_id","batch_id"],["receiving_batches.organization_id","receiving_batches.store_id","receiving_batches.id"],name="fk_receiving_decision_batch",ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id","store_id","discrepancy_id"],["receiving_discrepancies.organization_id","receiving_discrepancies.store_id","receiving_discrepancies.id"],name="fk_receiving_decision_discrepancy",ondelete="RESTRICT"),
        sa.CheckConstraint("decision IN ('accept_observation','request_recount','return_supplier_review','hold_identity','hold_price')",name="ck_receiving_decision_kind"),
        sa.CheckConstraint("batch_version>1 AND length(evidence_sha256)=64",name="ck_receiving_decision_proof"))
    op.create_table("receiving_commands",
        sa.Column("id",sa.String(36),primary_key=True),
        sa.Column("organization_id",sa.String(36),nullable=False),
        sa.Column("store_id",sa.String(36),nullable=False),
        sa.Column("actor_id",sa.String(36),nullable=False),
        sa.Column("operation",sa.String(32),nullable=False),
        sa.Column("idempotency_key",sa.String(200),nullable=False),
        sa.Column("payload_digest",sa.String(64),nullable=False),
        sa.Column("response",sa.JSON(),nullable=False),
        sa.Column("created_at",UTCDateTime(),nullable=False),
        sa.UniqueConstraint("organization_id","store_id","actor_id","operation","idempotency_key",name="uq_receiving_commands_identity"),
        sa.ForeignKeyConstraint(["organization_id","store_id"],["stores.organization_id","stores.id"],name="fk_receiving_commands_store",ondelete="RESTRICT"))
    bind=op.get_bind()
    if bind.dialect.name=="sqlite":
        for table in ("receiving_lines","receiving_discrepancies","receiving_decisions"):
            for action in ("UPDATE","DELETE"):
                op.execute(f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'receiving evidence is immutable'); END")
    elif bind.dialect.name=="postgresql":
        op.execute("CREATE FUNCTION deny_receiving_evidence_mutation() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'receiving evidence is immutable'; END; $$ LANGUAGE plpgsql")
        for table in ("receiving_lines","receiving_discrepancies","receiving_decisions"):
            op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION deny_receiving_evidence_mutation()")


def downgrade():
    bind=op.get_bind()
    for table in ("receiving_commands","receiving_decisions","receiving_discrepancies",
                  "receiving_lines","receiving_batches"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("0032 receiving downgrade refused with evidence; use forward-fix")
    for table in ("receiving_commands","receiving_decisions","receiving_discrepancies","receiving_lines"):
        op.drop_table(table)
    op.drop_index("ix_receiving_batches_scope_time",table_name="receiving_batches")
    op.drop_table("receiving_batches")
    if bind.dialect.name=="postgresql":
        op.execute("DROP FUNCTION deny_receiving_evidence_mutation()")
