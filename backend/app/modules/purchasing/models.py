"""Employee-imported purchasing evidence, separate from formal orders and stock."""
from datetime import datetime

from sqlalchemy import (Boolean, CheckConstraint, ForeignKeyConstraint, Index, Integer,
                        JSON, String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime


class PurchaseEvidence(Base):
    __tablename__ = "purchase_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    supplier_name: Mapped[str] = mapped_column(String(200), nullable=False)
    original_text: Mapped[str] = mapped_column(Text(), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    consent_confirmed: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_purchase_evidence_scope_id"),
        UniqueConstraint("organization_id", "store_id", "content_sha256", name="uq_purchase_evidence_digest"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                             name="fk_purchase_evidence_store", ondelete="RESTRICT"),
        CheckConstraint("source_kind IN ('wechat_paste','delivery_note_text')",
                        name="ck_purchase_evidence_source"),
        CheckConstraint("length(original_text) BETWEEN 1 AND 10000", name="ck_purchase_evidence_text"),
        CheckConstraint("length(supplier_name) BETWEEN 1 AND 200", name="ck_purchase_evidence_supplier"),
        CheckConstraint("length(content_sha256) = 64", name="ck_purchase_evidence_sha"),
        CheckConstraint("consent_confirmed = true", name="ck_purchase_evidence_consent"),
    )


class PurchaseIntent(Base):
    __tablename__ = "purchase_intents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(36), nullable=False)
    creator_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_purchase_intents_scope_id"),
        UniqueConstraint("organization_id", "store_id", "evidence_id", name="uq_purchase_intents_evidence"),
        ForeignKeyConstraint(["organization_id", "store_id", "evidence_id"],
                             ["purchase_evidence.organization_id", "purchase_evidence.store_id", "purchase_evidence.id"],
                             name="fk_purchase_intents_evidence", ondelete="RESTRICT"),
        CheckConstraint("status = 'awaiting_arrival'", name="ck_purchase_intents_status"),
        CheckConstraint("version > 0", name="ck_purchase_intents_version"),
        Index("ix_purchase_intents_queue", "organization_id", "store_id", "created_at"),
    )


class PurchaseIntentLine(Base):
    __tablename__ = "purchase_intent_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    intent_id: Mapped[str] = mapped_column(String(36), nullable=False)
    original_description: Mapped[str] = mapped_column(String(500), nullable=False)
    product_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    product_version: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    quantity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_purchase_lines_scope_id"),
        ForeignKeyConstraint(["organization_id", "store_id", "intent_id"],
                             ["purchase_intents.organization_id", "purchase_intents.store_id", "purchase_intents.id"],
                             name="fk_purchase_lines_intent", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "product_id"],
                             ["catalog_products.organization_id", "catalog_products.store_id", "catalog_products.id"],
                             name="fk_purchase_lines_product", ondelete="RESTRICT"),
        CheckConstraint("length(original_description) BETWEEN 1 AND 500", name="ck_purchase_lines_description"),
        CheckConstraint("(product_id IS NULL AND product_version IS NULL AND quantity IS NULL AND "
                        "confirmed_by IS NULL AND confirmed_at IS NULL) OR "
                        "(product_id IS NOT NULL AND product_version > 0 AND quantity IS NOT NULL AND "
                        "confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",
                        name="ck_purchase_lines_confirmed"),
        Index("ix_purchase_lines_intent", "organization_id", "store_id", "intent_id"),
    )


class PurchaseCommand(Base):
    __tablename__ = "purchase_commands"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict] = mapped_column(JSON(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "actor_id", "operation", "idempotency_key",
                         name="uq_purchase_commands_identity"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                             name="fk_purchase_commands_store", ondelete="RESTRICT"),
    )
