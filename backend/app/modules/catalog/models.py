from datetime import datetime

from sqlalchemy import (CheckConstraint, ForeignKeyConstraint, Index, Integer, JSON,
                        String, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime


class CatalogProduct(Base):
    """Stable external identity; source values live in immutable snapshots."""

    __tablename__ = "catalog_products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_system: Mapped[str] = mapped_column(String(40), nullable=False)
    external_code: Mapped[str] = mapped_column(String(120), nullable=False)
    source_version: Mapped[str] = mapped_column(String(80), nullable=False)
    source_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    current_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    barcode_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "source_system", "external_code",
                         name="uq_catalog_products_external_identity"),
        UniqueConstraint("organization_id", "store_id", "id", name="uq_catalog_products_scope_id"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                             name="fk_catalog_products_store_scope", ondelete="RESTRICT"),
        CheckConstraint("version > 0", name="ck_catalog_products_version_positive"),
        Index("ix_catalog_products_scope_barcode", "organization_id", "store_id", "active", "barcode_sha256"),
        Index("ix_catalog_products_scope_code", "organization_id", "store_id", "active", "external_code"),
        Index("ix_catalog_products_scope", "organization_id", "store_id", "source_system"),
    )


class CatalogSnapshot(Base):
    """Immutable original payload; dirty/zero/quote values are preserved verbatim."""

    __tablename__ = "catalog_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_version: Mapped[str] = mapped_column(String(80), nullable=False)
    source_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON(), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("product_id", "payload_sha256", name="uq_catalog_snapshots_payload"),
        UniqueConstraint("organization_id", "store_id", "id", name="uq_catalog_snapshots_scope_id"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                             name="fk_catalog_snapshots_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["product_id"], ["catalog_products.id"],
                             name="fk_catalog_snapshots_product", ondelete="CASCADE"),
        CheckConstraint("length(payload_sha256) = 64", name="ck_catalog_snapshots_sha"),
        Index("ix_catalog_snapshots_product", "product_id", "captured_at"),
    )


class CatalogExtension(Base):
    """V4-managed aliases/images/knowledge/local overrides; sync must not overwrite."""

    __tablename__ = "catalog_extensions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON(), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by: Mapped[str] = mapped_column(String(36), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("product_id", name="uq_catalog_extensions_product"),
        UniqueConstraint("organization_id", "store_id", "id", name="uq_catalog_extensions_scope_id"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                             name="fk_catalog_extensions_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["product_id"], ["catalog_products.id"],
                             name="fk_catalog_extensions_product", ondelete="CASCADE"),
        CheckConstraint("version > 0", name="ck_catalog_extensions_version_positive"),
    )


class CatalogConflict(Base):
    """Traceable source-vs-local disagreement; never auto-overwrites local edits."""

    __tablename__ = "catalog_conflicts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    field_name: Mapped[str] = mapped_column(String(80), nullable=False)
    source_value: Mapped[dict] = mapped_column(JSON(), nullable=False)
    local_value: Mapped[dict] = mapped_column(JSON(), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    resolved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_catalog_conflicts_scope_id"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                             name="fk_catalog_conflicts_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["product_id"], ["catalog_products.id"],
                             name="fk_catalog_conflicts_product", ondelete="CASCADE"),
        CheckConstraint("status IN ('pending', 'resolved', 'dismissed')",
                        name="ck_catalog_conflicts_status"),
        Index("ix_catalog_conflicts_scope_status", "organization_id", "store_id", "status"),
    )
