from datetime import datetime

from sqlalchemy import (CheckConstraint, ForeignKeyConstraint, Index, Integer,
                        String, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime


class MediaAsset(Base):
    """Immutable file bytes live outside DB; metadata is source/audit evidence."""

    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id",
                         name="uq_media_assets_scope_id"),
        UniqueConstraint("organization_id", "store_id", "sha256",
                         name="uq_media_assets_scope_sha"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_media_assets_store_scope", ondelete="RESTRICT"),
        CheckConstraint("category IN ('category_image', 'inventory_photo')",
                        name="ck_media_assets_category"),
        CheckConstraint("byte_size BETWEEN 1 AND 5242880", name="ck_media_assets_size"),
        CheckConstraint("length(sha256) = 64", name="ck_media_assets_sha"),
        Index("ix_media_assets_scope_created", "organization_id", "store_id", "created_at"),
    )
