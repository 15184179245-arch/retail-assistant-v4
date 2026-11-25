"""OFC-003: immutable, store-scoped extraction snapshots and human decisions."""
from datetime import datetime
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime


class OfficeExtraction(Base):
    __tablename__ = "office_extractions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(36), nullable=False)
    evidence_version: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_key: Mapped[str] = mapped_column(String(80), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(80), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    request_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_office_extraction_scope"),
        UniqueConstraint("organization_id", "store_id", "request_key", name="uq_office_extraction_request"),
        ForeignKeyConstraint(["organization_id", "store_id", "evidence_id"],
                             ["office_evidence.organization_id", "office_evidence.store_id",
                              "office_evidence.id"], name="fk_office_extraction_evidence", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "created_by"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_office_extraction_actor", ondelete="RESTRICT"),
        CheckConstraint("evidence_version = 1", name="ck_office_extraction_evidence_version"),
        CheckConstraint("length(evidence_sha256) = 64 AND length(content_sha256) = 64 "
                        "AND length(request_sha256) = 64", name="ck_office_extraction_hashes"),
        Index("ix_office_extraction_evidence", "organization_id", "store_id",
              "evidence_id", "created_at"),
    )


class OfficeFieldReview(Base):
    __tablename__ = "office_field_reviews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    extraction_id: Mapped[str] = mapped_column(String(36), nullable=False)
    extraction_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    field_id: Mapped[str] = mapped_column(String(80), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    corrected_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    reviewed_by: Mapped[str] = mapped_column(String(36), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    request_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_office_review_scope"),
        UniqueConstraint("organization_id", "store_id", "extraction_id", "field_id",
                         name="uq_office_review_field_once"),
        UniqueConstraint("organization_id", "store_id", "request_key",
                         name="uq_office_review_request"),
        ForeignKeyConstraint(["organization_id", "store_id", "extraction_id"],
                             ["office_extractions.organization_id", "office_extractions.store_id",
                              "office_extractions.id"], name="fk_office_review_extraction",
                             ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "reviewed_by"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_office_review_person", ondelete="RESTRICT"),
        CheckConstraint("decision IN ('confirmed','rejected','corrected')",
                        name="ck_office_review_decision"),
        CheckConstraint("expected_version = 0", name="ck_office_review_first_version"),
        CheckConstraint("length(extraction_sha256) = 64 AND length(request_sha256) = 64",
                        name="ck_office_review_sha"),
    )
