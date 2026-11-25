"""OFC-002 scoped immutable evidence and idempotent submission receipts."""
from datetime import datetime

from sqlalchemy import (CheckConstraint, ForeignKeyConstraint, Index, Integer,
                        String, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime


class OfficeEvidence(Base):
    __tablename__ = "office_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str] = mapped_column(String(80), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_channel: Mapped[str] = mapped_column(String(80), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id",
                         name="uq_office_evidence_scope"),
        UniqueConstraint("organization_id", "store_id", "sha256",
                         name="uq_office_evidence_digest"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_office_evidence_store", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "created_by"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_office_evidence_creator", ondelete="RESTRICT"),
        CheckConstraint("version = 1", name="ck_office_evidence_version"),
        CheckConstraint("byte_size BETWEEN 1 AND 12582912",
                        name="ck_office_evidence_size"),
        CheckConstraint("length(sha256) = 64", name="ck_office_evidence_hash"),
        Index("ix_office_evidence_scope_created",
              "organization_id", "store_id", "created_at", "id"),
    )


class OfficeIntakeReceipt(Base):
    __tablename__ = "office_intake_receipts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(36), nullable=False)
    submitted_by: Mapped[str] = mapped_column(String(36), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_channel: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id",
                         name="uq_office_receipt_scope"),
        UniqueConstraint("organization_id", "store_id", "idempotency_key",
                         name="uq_office_receipt_idempotency"),
        ForeignKeyConstraint(["organization_id", "store_id", "evidence_id"],
                             ["office_evidence.organization_id", "office_evidence.store_id",
                              "office_evidence.id"], name="fk_office_receipt_evidence",
                             ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "submitted_by"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_office_receipt_actor", ondelete="RESTRICT"),
        CheckConstraint("version = 1", name="ck_office_receipt_version"),
        CheckConstraint("length(request_digest) = 64", name="ck_office_receipt_hash"),
    )
