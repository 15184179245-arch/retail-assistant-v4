"""OFC-002: scoped immutable evidence and intake receipts.

Revision ID: 0034_office_intake
Revises: 0033_analytics_snapshots
"""
from alembic import op
import sqlalchemy as sa
from backend.app.modules.agents.models import UTCDateTime

revision = "0034_office_intake"
down_revision = "0033_analytics_snapshots"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "office_evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(80), nullable=False),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("source_channel", sa.String(80), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "id",
                            name="uq_office_evidence_scope"),
        sa.UniqueConstraint("organization_id", "store_id", "sha256",
                            name="uq_office_evidence_digest"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_office_evidence_store", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "created_by"],
                                ["subjects.organization_id", "subjects.id"],
                                name="fk_office_evidence_creator", ondelete="RESTRICT"),
        sa.CheckConstraint("version = 1", name="ck_office_evidence_version"),
        sa.CheckConstraint("byte_size BETWEEN 1 AND 12582912",
                           name="ck_office_evidence_size"),
        sa.CheckConstraint("length(sha256) = 64", name="ck_office_evidence_hash"),
    )
    op.create_index("ix_office_evidence_scope_created", "office_evidence",
                    ["organization_id", "store_id", "created_at", "id"])
    op.create_table(
        "office_intake_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("evidence_id", sa.String(36), nullable=False),
        sa.Column("submitted_by", sa.String(36), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("source_channel", sa.String(80), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("submitted_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "id",
                            name="uq_office_receipt_scope"),
        sa.UniqueConstraint("organization_id", "store_id", "idempotency_key",
                            name="uq_office_receipt_idempotency"),
        sa.ForeignKeyConstraint(
            ["organization_id", "store_id", "evidence_id"],
            ["office_evidence.organization_id", "office_evidence.store_id",
             "office_evidence.id"], name="fk_office_receipt_evidence",
            ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "submitted_by"],
                                ["subjects.organization_id", "subjects.id"],
                                name="fk_office_receipt_actor", ondelete="RESTRICT"),
        sa.CheckConstraint("version = 1", name="ck_office_receipt_version"),
        sa.CheckConstraint("length(request_digest) = 64", name="ck_office_receipt_hash"),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table in ("office_evidence", "office_intake_receipts"):
            op.execute(sa.text(f"""CREATE TRIGGER trg_{table}_no_update
                BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT,'office immutable evidence'); END"""))
            op.execute(sa.text(f"""CREATE TRIGGER trg_{table}_no_delete
                BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT,'office immutable evidence'); END"""))
    elif dialect == "postgresql":
        op.execute(sa.text("""CREATE FUNCTION deny_office_intake_mutation() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'office immutable evidence'; END; $$"""))
        for table in ("office_evidence", "office_intake_receipts"):
            op.execute(sa.text(f"""CREATE TRIGGER trg_{table}_immutable
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION deny_office_intake_mutation()"""))


def downgrade():
    conn = op.get_bind()
    if (conn.execute(sa.text("SELECT 1 FROM office_evidence LIMIT 1")).first()
            or conn.execute(sa.text("SELECT 1 FROM office_intake_receipts LIMIT 1")).first()):
        raise RuntimeError("0034 office intake downgrade refused with immutable evidence")
    op.drop_table("office_intake_receipts")
    op.drop_index("ix_office_evidence_scope_created", table_name="office_evidence")
    op.drop_table("office_evidence")
    if conn.dialect.name == "postgresql":
        op.execute(sa.text("DROP FUNCTION deny_office_intake_mutation()"))
