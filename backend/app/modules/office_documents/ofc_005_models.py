"""OFC-005: append-only office documents, independent human review and exports."""
from datetime import datetime
from sqlalchemy import (CheckConstraint, DDL, ForeignKeyConstraint, Index, Integer,
                        JSON, String, UniqueConstraint, event)
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime
from backend.app.modules.office_intake import ofc_002_models as evidence_models  # noqa: F401


class OfficeDocumentVersion(Base):
    __tablename__ = "office_document_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    document_key: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String(36))
    document_type: Mapped[str] = mapped_column(String(24), nullable=False)
    template_version: Mapped[str] = mapped_column(String(60), nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(36), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    submitted_by: Mapped[str] = mapped_column(String(36), nullable=False)
    request_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total_amount: Mapped[str] = mapped_column(String(40), nullable=False)
    contents: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id","store_id","id",name="uq_office_document_scope"),
        UniqueConstraint("organization_id","store_id","document_key","version",
                         name="uq_office_document_version"),
        UniqueConstraint("organization_id","store_id","request_key",
                         name="uq_office_document_request"),
        ForeignKeyConstraint(["organization_id","store_id"],["stores.organization_id","stores.id"],
                             name="fk_office_document_store",ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id","store_id","parent_id"],
            ["office_document_versions.organization_id","office_document_versions.store_id",
             "office_document_versions.id"],name="fk_office_document_parent",ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id","store_id","evidence_id"],
            ["office_evidence.organization_id","office_evidence.store_id",
             "office_evidence.id"],name="fk_office_document_evidence",ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id","submitted_by"],
            ["subjects.organization_id","subjects.id"],name="fk_office_document_author",ondelete="RESTRICT"),
        CheckConstraint("version > 0 AND ((version=1 AND parent_id IS NULL) OR "
                        "(version>1 AND parent_id IS NOT NULL))",name="ck_office_document_version"),
        CheckConstraint("document_type IN ('quotation','reconciliation','receiving_list')",
                        name="ck_office_document_type"),
        CheckConstraint("length(evidence_sha256)=64 AND length(request_digest)=64 "
                        "AND length(content_sha256)=64",name="ck_office_document_digests"),
        Index("ix_office_documents_scope_time","organization_id","store_id","created_at","id"),
    )


class OfficeDocumentReview(Base):
    __tablename__ = "office_document_reviews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    document_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(36), nullable=False)
    decision: Mapped[str] = mapped_column(String(12), nullable=False)
    document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    request_key: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id","store_id","document_version_id",
                         name="uq_office_document_review_once"),
        UniqueConstraint("organization_id","store_id","request_key",
                         name="uq_office_document_review_request"),
        ForeignKeyConstraint(["organization_id","store_id","document_version_id"],
            ["office_document_versions.organization_id","office_document_versions.store_id",
             "office_document_versions.id"],name="fk_office_document_review_version",ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id","decided_by"],
            ["subjects.organization_id","subjects.id"],name="fk_office_document_reviewer",ondelete="RESTRICT"),
        CheckConstraint("decision IN ('approved','rejected')",name="ck_office_document_review_choice"),
        CheckConstraint("length(document_sha256)=64",name="ck_office_document_review_digest"),
    )


class OfficeDocumentExport(Base):
    __tablename__ = "office_document_exports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    document_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    exported_by: Mapped[str] = mapped_column(String(36), nullable=False)
    file_format: Mapped[str] = mapped_column(String(8), nullable=False)
    request_key: Mapped[str] = mapped_column(String(120), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id","store_id","document_version_id","file_format",
                         name="uq_office_document_export_once"),
        UniqueConstraint("organization_id","store_id","request_key",
                         name="uq_office_document_export_request"),
        ForeignKeyConstraint(["organization_id","store_id","document_version_id"],
            ["office_document_versions.organization_id","office_document_versions.store_id",
             "office_document_versions.id"],name="fk_office_document_export_version",ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id","exported_by"],
            ["subjects.organization_id","subjects.id"],name="fk_office_document_exporter",ondelete="RESTRICT"),
        CheckConstraint("file_format IN ('xlsx','pdf')",name="ck_office_document_export_format"),
        CheckConstraint("length(content_sha256)=64 AND byte_size > 0",
                        name="ck_office_document_export_bytes"),
    )


def _guard(table, connection, **_kw):
    name = table.name
    if connection.dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            connection.execute(DDL(
                f"CREATE TRIGGER {name}_no_{action.lower()} BEFORE {action} ON {name} "
                "BEGIN SELECT RAISE(ABORT,'office document record immutable'); END"))
    elif connection.dialect.name == "postgresql":
        connection.execute(DDL(
            "CREATE OR REPLACE FUNCTION deny_office_document_mutation() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'office document record immutable'; END; $$ LANGUAGE plpgsql"))
        connection.execute(DDL(
            f"CREATE TRIGGER {name}_immutable BEFORE UPDATE OR DELETE ON {name} "
            "FOR EACH ROW EXECUTE FUNCTION deny_office_document_mutation()"))

for _table in (OfficeDocumentVersion.__table__, OfficeDocumentReview.__table__,
               OfficeDocumentExport.__table__):
    event.listen(_table,"after_create",_guard)
