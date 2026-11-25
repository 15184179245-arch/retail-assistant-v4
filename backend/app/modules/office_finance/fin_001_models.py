"""Immutable scoped workpaper versions and transaction-bound command receipts."""
from datetime import datetime
from sqlalchemy import CheckConstraint, DDL, ForeignKeyConstraint, Index, Integer, JSON, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime
from backend.app.modules.office_intake import ofc_002_models as evidence_models  # noqa: F401

class FinanceWorkpaper(Base):
    __tablename__ = "finance_workpapers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workpaper_key: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String(36))
    evidence_id: Mapped[str] = mapped_column(String(36), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    request_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    contents: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_fin_workpaper_scope"),
        UniqueConstraint("organization_id", "store_id", "workpaper_key", "version", name="uq_fin_workpaper_version"),
        UniqueConstraint("organization_id", "store_id", "request_key", name="uq_fin_workpaper_request"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_fin_workpaper_store", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "parent_id"], ["finance_workpapers.organization_id", "finance_workpapers.store_id", "finance_workpapers.id"], name="fk_fin_workpaper_parent", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "evidence_id"], ["office_evidence.organization_id", "office_evidence.store_id", "office_evidence.id"], name="fk_fin_workpaper_evidence", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "created_by"], ["subjects.organization_id", "subjects.id"], name="fk_fin_workpaper_author", ondelete="RESTRICT"),
        CheckConstraint("version > 0 AND ((version=1 AND parent_id IS NULL) OR (version>1 AND parent_id IS NOT NULL))", name="ck_fin_workpaper_version"),
        CheckConstraint("length(evidence_sha256)=64 AND length(request_digest)=64 AND length(content_sha256)=64", name="ck_fin_workpaper_digests"),
        Index("ix_fin_workpaper_scope_created", "organization_id", "store_id", "created_at", "id"),
    )

for action in ("UPDATE", "DELETE"):
    event.listen(FinanceWorkpaper.__table__, "after_create", DDL(
        f"CREATE TRIGGER finance_workpapers_no_{action.lower()} BEFORE {action} ON finance_workpapers "
        "BEGIN SELECT RAISE(ABORT,'finance workpaper immutable'); END").execute_if(dialect="sqlite"))
