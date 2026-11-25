from datetime import datetime

from sqlalchemy import (CheckConstraint, ForeignKeyConstraint, Index, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime


class ProductAlias(Base):
    """Manually bound secondary code/name/pinyin; revocable and auditable."""

    __tablename__ = "product_aliases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False)
    alias_value: Mapped[str] = mapped_column(String(120), nullable=False)
    alias_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "product_id", "alias_kind",
                         "alias_value", name="uq_product_aliases_binding"),
        UniqueConstraint("organization_id", "store_id", "id", name="uq_product_aliases_scope_id"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_product_aliases_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["product_id"], ["catalog_products.id"],
                             name="fk_product_aliases_product", ondelete="CASCADE"),
        CheckConstraint("alias_kind IN ('code', 'name', 'pinyin')",
                        name="ck_product_aliases_kind"),
        CheckConstraint("status IN ('active', 'revoked')",
                        name="ck_product_aliases_status"),
        Index("ix_product_aliases_lookup", "organization_id", "store_id",
              "alias_value", "status"),
    )


class SearchProjection(Base):
    """Rebuildable derived search row; never a replacement for source truth."""

    __tablename__ = "search_projections"

    product_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    code: Mapped[str] = mapped_column(String(120), nullable=False)
    barcode: Mapped[str | None] = mapped_column(String(200), nullable=True)
    normalized_barcode_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    search_text: Mapped[str] = mapped_column(Text(), nullable=False, default="", server_default="")
    snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    name_text: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    alias_text: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    pinyin_text: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "product_id",
                         name="uq_search_projections_scope_product"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_search_projections_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["product_id"], ["catalog_products.id"],
                             name="fk_search_projections_product", ondelete="CASCADE"),
        Index("ix_search_projections_normalized", "organization_id", "store_id", "normalized_barcode_sha256"),
        Index("ix_search_projections_scope", "organization_id", "store_id"),
    )
