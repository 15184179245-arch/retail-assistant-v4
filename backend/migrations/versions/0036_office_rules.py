"""OFC-004 immutable store scoped office rule versions + human decisions.

Revision ID: 0036_office_rules
Revises: 0035_office_extraction
"""
from alembic import op
import sqlalchemy as sa
from backend.app.modules.pricing.models import UTCDateTime

revision = "0036_office_rules"
down_revision = "0035_office_extraction"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "office_rule_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("rule_key", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("scope_key", sa.String(200), nullable=False),
        sa.Column("rule_body", sa.JSON(), nullable=False),
        sa.Column("source_ref", sa.String(200), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("valid_from", UTCDateTime(), nullable=False),
        sa.Column("valid_until", UTCDateTime(), nullable=True),
        sa.Column("submitted_by", sa.String(36), nullable=False),
        sa.Column("submit_key", sa.String(200), nullable=False),
        sa.Column("submit_digest", sa.String(64), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "id",
                            name="uq_office_rule_scope"),
        sa.UniqueConstraint("organization_id", "store_id", "rule_key", "version",
                            name="uq_office_rule_version"),
        sa.UniqueConstraint("organization_id", "store_id", "submit_key",
                            name="uq_office_rule_submit_key"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_office_rule_store", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "submitted_by"],
                                ["subjects.organization_id", "subjects.id"],
                                name="fk_office_rule_submitter", ondelete="RESTRICT"),
        sa.CheckConstraint("version > 0", name="ck_office_rule_version"),
        sa.CheckConstraint("kind IN ('customer_mapping','supplier_mapping',"
                           "'package_conversion','return_policy','exchange_policy')",
                           name="ck_office_rule_kind"),
        sa.CheckConstraint("length(source_sha256) = 64 AND length(body_sha256) = 64 "
                           "AND length(submit_digest) = 64", name="ck_office_rule_digests"),
        sa.CheckConstraint("valid_until IS NULL OR valid_until > valid_from",
                           name="ck_office_rule_window"),
    )
    op.create_index("ix_office_rule_lookup", "office_rule_versions",
                    ["organization_id", "store_id", "kind", "scope_key", "valid_from"])
    op.create_table(
        "office_rule_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("rule_id", sa.String(36), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("decision_version", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(2000), nullable=False),
        sa.Column("decided_by", sa.String(36), nullable=False),
        sa.Column("request_key", sa.String(200), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "rule_id", "decision_version",
                            name="uq_office_rule_decision_sequence"),
        sa.UniqueConstraint("organization_id", "store_id", "request_key",
                            name="uq_office_rule_decision_request"),
        sa.ForeignKeyConstraint(
            ["organization_id", "store_id", "rule_id"],
            ["office_rule_versions.organization_id", "office_rule_versions.store_id",
             "office_rule_versions.id"], name="fk_office_rule_decision_rule",
            ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "decided_by"],
                                ["subjects.organization_id", "subjects.id"],
                                name="fk_office_rule_decider", ondelete="RESTRICT"),
        sa.CheckConstraint("decision_version IN (1,2)",
                           name="ck_office_rule_decision_version"),
        sa.CheckConstraint("decision IN ('approved','rejected','revoked')",
                           name="ck_office_rule_decision_enum"),
        sa.CheckConstraint("length(input_sha256) = 64 AND length(request_digest) = 64",
                           name="ck_office_rule_decision_digest"),
    )
    op.create_index("ix_office_rule_decisions_latest", "office_rule_decisions",
                    ["organization_id", "store_id", "rule_id", "decision_version"])
    connection = op.get_bind()
    for table in ("office_rule_versions", "office_rule_decisions"):
        if connection.dialect.name == "sqlite":
            for action in ("UPDATE", "DELETE"):
                connection.exec_driver_sql(
                    f"CREATE TRIGGER trg_{table}_{action.lower()}_deny "
                    f"BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT,'office rule records are immutable'); END"
                )
        elif connection.dialect.name == "postgresql":
            fn = f"deny_{table}_mutation"
            connection.exec_driver_sql(
                f"CREATE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'office rule records are immutable'; END; $$"
            )
            connection.exec_driver_sql(
                f"CREATE TRIGGER trg_{table}_deny BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION {fn}()"
            )


def downgrade():
    connection = op.get_bind()
    for table in ("office_rule_decisions", "office_rule_versions"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("0036 office rule downgrade refused with immutable history")
    op.drop_index("ix_office_rule_decisions_latest", table_name="office_rule_decisions")
    op.drop_table("office_rule_decisions")
    op.drop_index("ix_office_rule_lookup", table_name="office_rule_versions")
    op.drop_table("office_rule_versions")
    if connection.dialect.name == "postgresql":
        for table in ("office_rule_decisions", "office_rule_versions"):
            connection.exec_driver_sql(f"DROP FUNCTION deny_{table}_mutation()")
