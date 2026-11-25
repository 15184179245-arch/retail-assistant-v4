"""ERP-002: additive local authorization & write-intent records (no ERP writes).

Revision ID: 0037_erp_command_gateway
Revises: 0036_office_rules
"""
from alembic import op
import sqlalchemy as sa
from backend.app.modules.agents.models import UTCDateTime

revision = "0037_erp_command_gateway"
down_revision = "0036_office_rules"
branch_labels = None
depends_on = None


def _scope(table):
    return sa.ForeignKeyConstraint(
        ["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
        name=f"fk_{table}_store", ondelete="RESTRICT")


def _person(table, field, name):
    return sa.ForeignKeyConstraint(
        ["organization_id", field], ["subjects.organization_id", "subjects.id"],
        name=name, ondelete="RESTRICT")


def upgrade():
    op.create_table(
        "erp_command_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("requested_by", sa.String(36), nullable=False),
        sa.Column("request_key", sa.String(200), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("target_kind", sa.String(40), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("target_digest", sa.String(64), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("parameters_digest", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_erp_request_scope"),
        sa.UniqueConstraint("organization_id", "store_id", "request_key", name="uq_erp_request_key"),
        _scope("erp_request"), _person("erp_request", "requested_by", "fk_erp_request_person"),
        sa.CheckConstraint("expected_version > 0", name="ck_erp_request_version"),
        sa.CheckConstraint("length(target_digest)=64 AND length(evidence_digest)=64 "
                           "AND length(parameters_digest)=64 AND length(request_digest)=64",
                           name="ck_erp_request_digests"),
        sa.CheckConstraint("action IN ('catalog_field_proposal','purchase_document_proposal',"
                           "'sales_return_proposal')", name="ck_erp_request_action"),
    )
    op.create_table(
        "erp_command_approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("approved_by", sa.String(36), nullable=False),
        sa.Column("decision", sa.String(12), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("approval_key", sa.String(200), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_erp_approval_scope"),
        sa.UniqueConstraint("organization_id", "store_id", "request_id", name="uq_erp_approval_once"),
        sa.UniqueConstraint("organization_id", "store_id", "approval_key", name="uq_erp_approval_key"),
        sa.ForeignKeyConstraint(
            ["organization_id", "store_id", "request_id"],
            ["erp_command_requests.organization_id", "erp_command_requests.store_id",
             "erp_command_requests.id"], name="fk_erp_approval_request", ondelete="RESTRICT"),
        _person("erp_approval", "approved_by", "fk_erp_approval_person"),
        sa.CheckConstraint("decision IN ('approved','rejected')", name="ck_erp_approval_decision"),
        sa.CheckConstraint("length(request_digest)=64", name="ck_erp_approval_digest"),
    )
    op.create_table(
        "erp_command_intents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("approval_id", sa.String(36), nullable=False),
        sa.Column("executor_id", sa.String(36), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("target_digest", sa.String(64), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_erp_intent_scope"),
        sa.UniqueConstraint("organization_id", "store_id", "request_id", name="uq_erp_intent_once"),
        sa.UniqueConstraint("organization_id", "store_id", "approval_id",
                            name="uq_erp_intent_approval_once"),
        sa.UniqueConstraint("organization_id", "store_id", "idempotency_key",
                            name="uq_erp_intent_key"),
        sa.ForeignKeyConstraint(
            ["organization_id", "store_id", "request_id"],
            ["erp_command_requests.organization_id", "erp_command_requests.store_id",
             "erp_command_requests.id"], name="fk_erp_intent_request", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["organization_id", "store_id", "approval_id"],
            ["erp_command_approvals.organization_id", "erp_command_approvals.store_id",
             "erp_command_approvals.id"], name="fk_erp_intent_approval", ondelete="RESTRICT"),
        _person("erp_intent", "executor_id", "fk_erp_intent_executor"),
        sa.CheckConstraint("length(request_digest)=64 AND length(target_digest)=64",
                           name="ck_erp_intent_digests"),
    )
    op.create_table(
        "erp_command_outcomes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("intent_id", sa.String(36), nullable=False),
        sa.Column("verified_by", sa.String(36), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("evidence_ref", sa.String(200), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("request_key", sa.String(200), nullable=False),
        sa.Column("occurred_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "intent_id", name="uq_erp_outcome_once"),
        sa.UniqueConstraint("organization_id", "store_id", "request_key", name="uq_erp_outcome_key"),
        sa.ForeignKeyConstraint(
            ["organization_id", "store_id", "intent_id"],
            ["erp_command_intents.organization_id", "erp_command_intents.store_id",
             "erp_command_intents.id"], name="fk_erp_outcome_intent", ondelete="RESTRICT"),
        _person("erp_outcome", "verified_by", "fk_erp_outcome_verifier"),
        sa.CheckConstraint("outcome IN ('unknown','manual_rejected','verified_external')",
                           name="ck_erp_outcome_code"),
        sa.CheckConstraint("length(evidence_sha256)=64", name="ck_erp_outcome_hash"),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table in ("erp_command_requests", "erp_command_approvals",
                      "erp_command_intents", "erp_command_outcomes"):
            op.execute(sa.text(f"""CREATE TRIGGER trg_{table}_no_update
                BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, 'ERP command evidence immutable'); END"""))
            op.execute(sa.text(f"""CREATE TRIGGER trg_{table}_no_delete
                BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, 'ERP command evidence immutable'); END"""))
    elif dialect == "postgresql":
        op.execute(sa.text("""CREATE FUNCTION deny_erp_command_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'ERP command evidence immutable'; END; $$"""))
        for table in ("erp_command_requests", "erp_command_approvals",
                      "erp_command_intents", "erp_command_outcomes"):
            op.execute(sa.text(f"""CREATE TRIGGER trg_{table}_immutable
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION deny_erp_command_mutation()"""))


def downgrade():
    bind = op.get_bind()
    for table in ("erp_command_outcomes", "erp_command_intents",
                  "erp_command_approvals", "erp_command_requests"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("0037 downgrade refused with ERP authorization history; forward-fix")
    for table in ("erp_command_outcomes", "erp_command_intents",
                  "erp_command_approvals", "erp_command_requests"):
        op.drop_table(table)
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("DROP FUNCTION deny_erp_command_mutation()"))
