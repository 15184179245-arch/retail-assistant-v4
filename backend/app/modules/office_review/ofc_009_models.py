"""OFC-009 unified office review items and immutable decision receipts."""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime


class OfficeReviewItem(Base):
    __tablename__ = "office_review_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(String(1000), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    diff_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    review_decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    review_notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    modifications: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    receipt_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_ori_scope"),
        UniqueConstraint("organization_id", "store_id", "target_kind", "target_id", name="uq_ori_target"),
        ForeignKeyConstraint(
            ["organization_id", "store_id"],
            ["stores.organization_id", "stores.id"],
            name="fk_ori_store",
            ondelete="RESTRICT",
        ),
        CheckConstraint("risk_level IN ('low', 'medium', 'high', 'critical')", name="ck_ori_risk"),
        CheckConstraint(
            "state IN ('pending', 'claimed', 'approved', 'rejected', 'modified', 'escalated')",
            name="ck_ori_state",
        ),
        CheckConstraint("target_version >= 1", name="ck_ori_target_ver"),
        Index("ix_ori_state_risk", "organization_id", "store_id", "state", "risk_level"),
    )


class OfficeReviewDecision(Base):
    __tablename__ = "office_review_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    item_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_version: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    modifications: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    receipt_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_ord_scope"),
        ForeignKeyConstraint(
            ["organization_id", "store_id"],
            ["stores.organization_id", "stores.id"],
            name="fk_ord_store",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "store_id", "item_id"],
            ["office_review_items.organization_id", "office_review_items.store_id", "office_review_items.id"],
            name="fk_ord_item",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "actor_id"],
            ["subjects.organization_id", "subjects.id"],
            name="fk_ord_actor",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "decision IN ('approved', 'rejected', 'modified', 'escalated')",
            name="ck_ord_decision",
        ),
        CheckConstraint("length(receipt_digest)=64", name="ck_ord_digest"),
        Index("ix_ord_item_decided", "organization_id", "store_id", "item_id", "decided_at"),
    )


def _immutable_decisions(target, connection, **kw):
    table = target.name
    if connection.dialect.name == "sqlite":
        for op in ("update", "delete"):
            connection.exec_driver_sql(
                f"CREATE TRIGGER trg_{table}_{op}_deny BEFORE {op} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, 'office review decision immutable'); END"
            )
    elif connection.dialect.name == "postgresql":
        connection.exec_driver_sql(
            f"CREATE FUNCTION deny_{table}_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
            f"BEGIN RAISE EXCEPTION 'office review decision immutable'; END; $$"
        )
        connection.exec_driver_sql(
            f"CREATE TRIGGER trg_{table}_deny BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION deny_{table}_mutation()"
        )


event.listen(OfficeReviewDecision.__table__, "after_create", _immutable_decisions)
