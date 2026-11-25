"""Sales draft, confirmed lines and handoff intent (SALE-002).

Four independent axes: stage (draft/ready), status (pending/paid), sync_state
(local_only/pending/synced/conflict), and handoff_state. Void/expiry are fields,
never extra stage/status values.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (Boolean, CheckConstraint, ForeignKeyConstraint, Index, Integer, JSON,
                        String, UniqueConstraint, text)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime
from backend.app.modules.pricing import models as pricing_models  # noqa: F401  (FK metadata)


class SalesDraft(Base):
    __tablename__ = "sales_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    creator_id: Mapped[str] = mapped_column(String(36), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    quote_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    stage: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", server_default="draft")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    sync_state: Mapped[str] = mapped_column(String(16), nullable=False, default="local_only", server_default="local_only")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total_amount: Mapped[str] = mapped_column(String(40), nullable=False, default="0.00", server_default="0.00")
    voided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_sales_drafts_scope_id"),
        UniqueConstraint("organization_id", "store_id", "idempotency_key", name="uq_sales_drafts_key"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                             name="fk_sales_drafts_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "creator_id"], ["subjects.organization_id", "subjects.id"],
                             name="fk_sales_drafts_creator_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "quote_id"],
                             ["pricing_quotes.organization_id", "pricing_quotes.store_id", "pricing_quotes.id"],
                             name="fk_sales_drafts_quote_scope", ondelete="RESTRICT"),
        CheckConstraint("stage IN ('draft', 'ready')", name="ck_sales_drafts_stage"),
        CheckConstraint("status IN ('pending', 'paid')", name="ck_sales_drafts_status"),
        CheckConstraint("sync_state IN ('local_only', 'pending', 'synced', 'conflict')",
                        name="ck_sales_drafts_sync_state"),
        CheckConstraint("version > 0", name="ck_sales_drafts_version_positive"),
        CheckConstraint("length(currency) = 3 AND currency = upper(currency)", name="ck_sales_drafts_currency"),
        CheckConstraint("length(total_amount) BETWEEN 1 AND 40", name="ck_sales_drafts_total"),
        Index("ix_sales_drafts_scope_time", "organization_id", "store_id", "created_at", "id"),
    )


class SalesDraftLine(Base):
    __tablename__ = "sales_draft_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    draft_id: Mapped[str] = mapped_column(String(36), nullable=False)
    quote_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    line_key: Mapped[str] = mapped_column(String(200), nullable=False)
    product_id: Mapped[str] = mapped_column(String(200), nullable=False)
    # Additive v2 identity. Legacy rows stay NULL; never guess an upstream ID/unit.
    catalog_product_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(80), nullable=True)
    quote_v2_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[str] = mapped_column(String(40), nullable=False)
    unit_price: Mapped[str] = mapped_column(String(40), nullable=False)
    discount_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    line_amount: Mapped[str] = mapped_column(String(40), nullable=False)
    confirmed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_sales_draft_lines_scope_id"),
        UniqueConstraint("organization_id", "store_id", "draft_id", "line_key",
                         name="uq_sales_draft_lines_draft_key"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                             name="fk_sales_draft_lines_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "draft_id"],
                             ["sales_drafts.organization_id", "sales_drafts.store_id", "sales_drafts.id"],
                             name="fk_sales_draft_lines_draft_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "confirmed_by"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_sales_draft_lines_confirmer_scope", ondelete="RESTRICT"),
        CheckConstraint("discount_bps BETWEEN 0 AND 10000", name="ck_sales_draft_lines_discount"),
        CheckConstraint("length(quantity) BETWEEN 1 AND 40 AND length(unit_price) BETWEEN 1 AND 40 "
                        "AND length(line_amount) BETWEEN 1 AND 40", name="ck_sales_draft_lines_money"),
        CheckConstraint("(confirmed_by IS NULL AND confirmed_at IS NULL) OR "
                        "(confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",
                        name="ck_sales_draft_lines_confirmation"),
        ForeignKeyConstraint(["organization_id", "store_id", "quote_id"],
            ["pricing_quotes.organization_id", "pricing_quotes.store_id", "pricing_quotes.id"],
            name="fk_sales_lines_quote_scope", ondelete="RESTRICT"),
        Index("ix_sales_draft_lines_draft", "organization_id", "store_id", "draft_id"),
    )


class SalesHandoff(Base):
    __tablename__ = "sales_handoffs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    draft_id: Mapped[str] = mapped_column(String(36), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    handoff_state: Mapped[str] = mapped_column(String(24), nullable=False)
    delivery_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_sales_handoffs_scope_id"),
        UniqueConstraint("organization_id", "store_id", "idempotency_key",
                         name="uq_sales_handoffs_key"),
        UniqueConstraint("organization_id", "store_id", "draft_id",
                         name="uq_sales_handoffs_one_per_draft"),
        UniqueConstraint("organization_id", "store_id", "external_ref",
                         name="uq_sales_handoffs_external_ref"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                             name="fk_sales_handoffs_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "draft_id"],
                             ["sales_drafts.organization_id", "sales_drafts.store_id", "sales_drafts.id"],
                             name="fk_sales_handoffs_draft_scope", ondelete="RESTRICT"),
        CheckConstraint("handoff_state IN ('requested', 'sent', 'receipt_unknown', 'confirmed', 'failed')",
                        name="ck_sales_handoffs_state"),
        CheckConstraint("length(external_ref) BETWEEN 1 AND 200", name="ck_sales_handoffs_ref"),
        Index("ix_sales_handoffs_draft", "organization_id", "store_id", "draft_id"),
    )


class SalesReceipt(Base):
    """Every accepted receipt key is append-only and replayable."""

    __tablename__ = "sales_receipts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    handoff_id: Mapped[str] = mapped_column(String(36), nullable=False)
    receipt_key: Mapped[str] = mapped_column(String(200), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    external_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    paid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    unknown: Mapped[bool] = mapped_column(Boolean, nullable=False)
    state_after: Mapped[str] = mapped_column(String(24), nullable=False)
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_sales_receipts_scope_id"),
        UniqueConstraint("organization_id", "store_id", "receipt_key",
                         name="uq_sales_receipts_key"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                             name="fk_sales_receipts_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "handoff_id"],
                             ["sales_handoffs.organization_id", "sales_handoffs.store_id", "sales_handoffs.id"],
                             name="fk_sales_receipts_handoff_scope", ondelete="RESTRICT"),
        CheckConstraint("state_after IN ('sent', 'receipt_unknown', 'confirmed')", name="ck_sales_receipts_state"),
        CheckConstraint("length(receipt_key) BETWEEN 1 AND 200 AND length(digest) = 64",
                        name="ck_sales_receipts_identity"),
        Index("ix_sales_receipts_handoff", "organization_id", "store_id", "handoff_id"),
    )


class PosDispatch(Base):
    """Durable intent before the external effect. An intent is never sent twice."""
    __tablename__ = "pos_dispatches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # stable command = handoff id
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    draft_id: Mapped[str] = mapped_column(String(36), nullable=False)
    draft_version: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    amount: Mapped[str] = mapped_column(String(40), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    delivery_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id", "id"],
            ["sales_handoffs.organization_id", "sales_handoffs.store_id", "sales_handoffs.id"],
            name="fk_pos_dispatch_handoff_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "draft_id"],
            ["sales_drafts.organization_id", "sales_drafts.store_id", "sales_drafts.id"],
            name="fk_pos_dispatch_draft_scope", ondelete="RESTRICT"),
        CheckConstraint("state IN ('dispatching', 'delivered', 'unknown')", name="ck_pos_dispatch_state"),
    )


class SalesLineCommandV2(Base):
    """One immutable command result per draft line key; not an external POS receipt."""
    __tablename__ = "sales_line_commands_v2"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    draft_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    line_key: Mapped[str] = mapped_column(String(200), nullable=False)
    quote_id: Mapped[str] = mapped_column(String(36), nullable=False)
    line_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "draft_id", "line_key", name="uq_line_command_v2_key"),
        ForeignKeyConstraint(["organization_id", "store_id", "draft_id"], ["sales_drafts.organization_id", "sales_drafts.store_id", "sales_drafts.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "quote_id"], ["pricing_quotes_v2.organization_id", "pricing_quotes_v2.store_id", "pricing_quotes_v2.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "line_id"], ["sales_draft_lines.organization_id", "sales_draft_lines.store_id", "sales_draft_lines.id"], ondelete="RESTRICT"),
        CheckConstraint("length(request_digest) = 64", name="ck_line_command_v2_digest"),
    )


from sqlalchemy import event
from backend.app.modules.pricing.models import _immutable_v2

event.listen(SalesLineCommandV2, "before_update", _immutable_v2)
event.listen(SalesLineCommandV2, "before_delete", _immutable_v2)
