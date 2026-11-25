"""OFC-005 append-only document versions, human reviews and export receipts.
Revision ID: 0039_office_documents
Revises: 0038_purchase_recommendations
"""
from alembic import op
import sqlalchemy as sa
from backend.app.modules.agents.models import UTCDateTime

revision = "0039_office_documents"
down_revision = "0038_purchase_recommendations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("office_document_versions",
        sa.Column("id",sa.String(36),primary_key=True),
        sa.Column("organization_id",sa.String(36),nullable=False),
        sa.Column("store_id",sa.String(36),nullable=False),
        sa.Column("document_key",sa.String(120),nullable=False),
        sa.Column("version",sa.Integer(),nullable=False),
        sa.Column("parent_id",sa.String(36)),
        sa.Column("document_type",sa.String(24),nullable=False),
        sa.Column("template_version",sa.String(60),nullable=False),
        sa.Column("evidence_id",sa.String(36),nullable=False),
        sa.Column("evidence_sha256",sa.String(64),nullable=False),
        sa.Column("submitted_by",sa.String(36),nullable=False),
        sa.Column("request_key",sa.String(120),nullable=False),
        sa.Column("request_digest",sa.String(64),nullable=False),
        sa.Column("content_sha256",sa.String(64),nullable=False),
        sa.Column("currency",sa.String(3),nullable=False),
        sa.Column("total_amount",sa.String(40),nullable=False),
        sa.Column("contents",sa.JSON(),nullable=False),
        sa.Column("created_at",UTCDateTime(),nullable=False),
        sa.UniqueConstraint("organization_id","store_id","id",name="uq_office_document_scope"),
        sa.UniqueConstraint("organization_id","store_id","document_key","version",
                            name="uq_office_document_version"),
        sa.UniqueConstraint("organization_id","store_id","request_key",
                            name="uq_office_document_request"),
        sa.ForeignKeyConstraint(["organization_id","store_id"],
            ["stores.organization_id","stores.id"],name="fk_office_document_store",ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id","store_id","parent_id"],
            ["office_document_versions.organization_id","office_document_versions.store_id",
             "office_document_versions.id"],name="fk_office_document_parent",ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id","store_id","evidence_id"],
            ["office_evidence.organization_id","office_evidence.store_id",
             "office_evidence.id"],name="fk_office_document_evidence",ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id","submitted_by"],
            ["subjects.organization_id","subjects.id"],name="fk_office_document_author",ondelete="RESTRICT"),
        sa.CheckConstraint("version > 0 AND ((version=1 AND parent_id IS NULL) OR "
                           "(version>1 AND parent_id IS NOT NULL))",name="ck_office_document_version"),
        sa.CheckConstraint("document_type IN ('quotation','reconciliation','receiving_list')",
                           name="ck_office_document_type"),
        sa.CheckConstraint("length(evidence_sha256)=64 AND length(request_digest)=64 "
                           "AND length(content_sha256)=64",name="ck_office_document_digests"))
    op.create_index("ix_office_documents_scope_time","office_document_versions",
                    ["organization_id","store_id","created_at","id"])
    op.create_table("office_document_reviews",
        sa.Column("id",sa.String(36),primary_key=True),
        sa.Column("organization_id",sa.String(36),nullable=False),
        sa.Column("store_id",sa.String(36),nullable=False),
        sa.Column("document_version_id",sa.String(36),nullable=False),
        sa.Column("decided_by",sa.String(36),nullable=False),
        sa.Column("decision",sa.String(12),nullable=False),
        sa.Column("document_sha256",sa.String(64),nullable=False),
        sa.Column("reason",sa.String(1000),nullable=False),
        sa.Column("request_key",sa.String(120),nullable=False),
        sa.Column("created_at",UTCDateTime(),nullable=False),
        sa.UniqueConstraint("organization_id","store_id","document_version_id",
                            name="uq_office_document_review_once"),
        sa.UniqueConstraint("organization_id","store_id","request_key",
                            name="uq_office_document_review_request"),
        sa.ForeignKeyConstraint(["organization_id","store_id","document_version_id"],
            ["office_document_versions.organization_id","office_document_versions.store_id",
             "office_document_versions.id"],name="fk_office_document_review_version",ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id","decided_by"],
            ["subjects.organization_id","subjects.id"],name="fk_office_document_reviewer",ondelete="RESTRICT"),
        sa.CheckConstraint("decision IN ('approved','rejected')",name="ck_office_document_review_choice"),
        sa.CheckConstraint("length(document_sha256)=64",name="ck_office_document_review_digest"))
    op.create_table("office_document_exports",
        sa.Column("id",sa.String(36),primary_key=True),
        sa.Column("organization_id",sa.String(36),nullable=False),
        sa.Column("store_id",sa.String(36),nullable=False),
        sa.Column("document_version_id",sa.String(36),nullable=False),
        sa.Column("exported_by",sa.String(36),nullable=False),
        sa.Column("file_format",sa.String(8),nullable=False),
        sa.Column("request_key",sa.String(120),nullable=False),
        sa.Column("content_sha256",sa.String(64),nullable=False),
        sa.Column("byte_size",sa.Integer(),nullable=False),
        sa.Column("created_at",UTCDateTime(),nullable=False),
        sa.UniqueConstraint("organization_id","store_id","document_version_id","file_format",
                            name="uq_office_document_export_once"),
        sa.UniqueConstraint("organization_id","store_id","request_key",
                            name="uq_office_document_export_request"),
        sa.ForeignKeyConstraint(["organization_id","store_id","document_version_id"],
            ["office_document_versions.organization_id","office_document_versions.store_id",
             "office_document_versions.id"],name="fk_office_document_export_version",ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id","exported_by"],
            ["subjects.organization_id","subjects.id"],name="fk_office_document_exporter",ondelete="RESTRICT"),
        sa.CheckConstraint("file_format IN ('xlsx','pdf')",name="ck_office_document_export_format"),
        sa.CheckConstraint("length(content_sha256)=64 AND byte_size > 0",
                           name="ck_office_document_export_bytes"))
    bind=op.get_bind()
    if bind.dialect.name=="sqlite":
        for table in ("office_document_versions","office_document_reviews","office_document_exports"):
            for action in ("UPDATE","DELETE"):
                op.execute(sa.text(
                    f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT,'office document record immutable'); END"))
    elif bind.dialect.name=="postgresql":
        op.execute(sa.text(
            "CREATE FUNCTION deny_office_document_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'office document record immutable'; END; $$"))
        for table in ("office_document_versions","office_document_reviews","office_document_exports"):
            op.execute(sa.text(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION deny_office_document_mutation()"))


def downgrade():
    bind=op.get_bind()
    for table in ("office_document_exports","office_document_reviews","office_document_versions"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("0039 downgrade refused with office document history; forward-fix")
    op.drop_table("office_document_exports")
    op.drop_table("office_document_reviews")
    op.drop_index("ix_office_documents_scope_time",table_name="office_document_versions")
    op.drop_table("office_document_versions")
    if bind.dialect.name=="postgresql":
        op.execute(sa.text("DROP FUNCTION deny_office_document_mutation()"))
