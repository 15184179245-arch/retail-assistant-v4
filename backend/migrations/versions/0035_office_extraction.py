"""OFC-003 immutable extraction/review receipts.

Revision ID: 0035_office_extraction
Revises: 0034_office_intake
"""
from alembic import op
import sqlalchemy as sa
from backend.app.modules.agents.models import UTCDateTime

revision = "0035_office_extraction"
down_revision = "0034_office_intake"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "office_extractions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("evidence_id", sa.String(36), nullable=False),
        sa.Column("evidence_version", sa.Integer(), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("extractor_key", sa.String(80), nullable=False),
        sa.Column("extractor_version", sa.String(80), nullable=False),
        sa.Column("source_kind", sa.String(20), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("request_key", sa.String(200), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_office_extraction_scope"),
        sa.UniqueConstraint("organization_id", "store_id", "request_key",
                            name="uq_office_extraction_request"),
        sa.ForeignKeyConstraint(
            ["organization_id", "store_id", "evidence_id"],
            ["office_evidence.organization_id", "office_evidence.store_id",
             "office_evidence.id"], name="fk_office_extraction_evidence",
            ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by"], ["subjects.organization_id", "subjects.id"],
            name="fk_office_extraction_actor", ondelete="RESTRICT"),
        sa.CheckConstraint("evidence_version = 1", name="ck_office_extraction_evidence_version"),
        sa.CheckConstraint("length(evidence_sha256) = 64 AND length(content_sha256) = 64 "
                           "AND length(request_sha256) = 64", name="ck_office_extraction_hashes"),
    )
    op.create_index("ix_office_extraction_evidence", "office_extractions",
                    ["organization_id", "store_id", "evidence_id", "created_at"])
    op.create_table(
        "office_field_reviews",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("extraction_id", sa.String(36), nullable=False),
        sa.Column("extraction_sha256", sa.String(64), nullable=False),
        sa.Column("field_id", sa.String(80), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("corrected_text", sa.String(500), nullable=True),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("reviewed_by", sa.String(36), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("request_key", sa.String(200), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_office_review_scope"),
        sa.UniqueConstraint("organization_id", "store_id", "extraction_id", "field_id",
                            name="uq_office_review_field_once"),
        sa.UniqueConstraint("organization_id", "store_id", "request_key",
                            name="uq_office_review_request"),
        sa.ForeignKeyConstraint(
            ["organization_id", "store_id", "extraction_id"],
            ["office_extractions.organization_id", "office_extractions.store_id",
             "office_extractions.id"], name="fk_office_review_extraction",
            ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["organization_id", "reviewed_by"], ["subjects.organization_id", "subjects.id"],
            name="fk_office_review_person", ondelete="RESTRICT"),
        sa.CheckConstraint("decision IN ('confirmed','rejected','corrected')",
                           name="ck_office_review_decision"),
        sa.CheckConstraint("expected_version = 0", name="ck_office_review_first_version"),
        sa.CheckConstraint("length(extraction_sha256) = 64 AND length(request_sha256) = 64",
                           name="ck_office_review_sha"),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for name in ("office_extractions", "office_field_reviews"):
            op.execute(sa.text(f"""CREATE TRIGGER trg_{name}_no_update
                BEFORE UPDATE ON {name}
                BEGIN SELECT RAISE(ABORT,'office extraction immutable'); END"""))
            op.execute(sa.text(f"""CREATE TRIGGER trg_{name}_no_delete
                BEFORE DELETE ON {name}
                BEGIN SELECT RAISE(ABORT,'office extraction immutable'); END"""))
    elif dialect == "postgresql":
        op.execute(sa.text("""CREATE FUNCTION deny_office_extraction_mutation() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'office extraction immutable'; END; $$"""))
        for name in ("office_extractions", "office_field_reviews"):
            op.execute(sa.text(f"""CREATE TRIGGER trg_{name}_immutable
                BEFORE UPDATE OR DELETE ON {name}
                FOR EACH ROW EXECUTE FUNCTION deny_office_extraction_mutation()"""))


def downgrade():
    conn = op.get_bind()
    for name in ("office_field_reviews", "office_extractions"):
        if conn.execute(sa.text(f"SELECT 1 FROM {name} LIMIT 1")).first():
            raise RuntimeError("0035 office extraction downgrade refused with immutable records")
    op.drop_table("office_field_reviews")
    op.drop_index("ix_office_extraction_evidence", table_name="office_extractions")
    op.drop_table("office_extractions")
    if conn.dialect.name == "postgresql":
        op.execute(sa.text("DROP FUNCTION deny_office_extraction_mutation()"))
