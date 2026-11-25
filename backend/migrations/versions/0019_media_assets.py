"""Immutable media metadata (STK-001).

Revision ID: 0019_media_assets
Revises: 0018_sales_handoff_delivery_ref
Downgrade refused while media rows exist; bytes live outside DB.
"""
from alembic import op
import sqlalchemy as sa

from backend.app.modules.agents.models import UTCDateTime

revision = "0019_media_assets"
down_revision = "0018_sales_handoff_delivery_ref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_assets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(500), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_media_assets_store_scope", ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_media_assets_scope_id"),
        sa.UniqueConstraint("organization_id", "store_id", "sha256", name="uq_media_assets_scope_sha"),
        sa.CheckConstraint("category IN ('category_image', 'inventory_photo')",
                           name="ck_media_assets_category"),
        sa.CheckConstraint("byte_size BETWEEN 1 AND 5242880", name="ck_media_assets_size"),
        sa.CheckConstraint("length(sha256) = 64", name="ck_media_assets_sha"),
    )
    op.create_index("ix_media_assets_scope_created", "media_assets",
                    ["organization_id", "store_id", "created_at"])


def downgrade() -> None:
    connection = op.get_bind()
    count = connection.execute(sa.text("SELECT COUNT(*) FROM media_assets")).scalar()
    if count:
        raise RuntimeError("media assets downgrade refused while rows exist")
    op.drop_index("ix_media_assets_scope_created", table_name="media_assets")
    op.drop_table("media_assets")
