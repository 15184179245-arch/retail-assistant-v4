"""Catalog identity, snapshots, extensions and conflicts (CAT-002).

Revision ID: 0010_catalog
Revises: 0009_knowledge
"""

from alembic import op
import sqlalchemy as sa

from backend.app.modules.agents.models import UTCDateTime

revision = "0010_catalog"
down_revision = "0009_knowledge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_products",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("source_system", sa.String(40), nullable=False),
        sa.Column("external_code", sa.String(120), nullable=False),
        sa.Column("source_version", sa.String(80), nullable=False),
        sa.Column("source_updated_at", UTCDateTime()),
        sa.Column("current_snapshot_id", sa.String(36)),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_catalog_products_store_scope", ondelete="RESTRICT"),
        sa.CheckConstraint("version > 0", name="ck_catalog_products_version_positive"),
        sa.UniqueConstraint("organization_id", "store_id", "source_system", "external_code",
                            name="uq_catalog_products_external_identity"),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_catalog_products_scope_id"),
    )
    op.create_index("ix_catalog_products_scope", "catalog_products",
                    ["organization_id", "store_id", "source_system"])
    op.create_table(
        "catalog_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("product_id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("source_version", sa.String(80), nullable=False),
        sa.Column("source_updated_at", UTCDateTime()),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("captured_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_catalog_snapshots_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["catalog_products.id"],
                                name="fk_catalog_snapshots_product", ondelete="CASCADE"),
        sa.CheckConstraint("length(payload_sha256) = 64", name="ck_catalog_snapshots_sha"),
        sa.UniqueConstraint("product_id", "payload_sha256", name="uq_catalog_snapshots_payload"),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_catalog_snapshots_scope_id"),
    )
    op.create_index("ix_catalog_snapshots_product", "catalog_snapshots",
                    ["product_id", "captured_at"])
    op.create_table(
        "catalog_extensions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("product_id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(36), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_catalog_extensions_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["catalog_products.id"],
                                name="fk_catalog_extensions_product", ondelete="CASCADE"),
        sa.CheckConstraint("version > 0", name="ck_catalog_extensions_version_positive"),
        sa.UniqueConstraint("product_id", name="uq_catalog_extensions_product"),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_catalog_extensions_scope_id"),
    )
    op.create_table(
        "catalog_conflicts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("product_id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("field_name", sa.String(80), nullable=False),
        sa.Column("source_value", sa.JSON(), nullable=False),
        sa.Column("local_value", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("resolved_by", sa.String(36)),
        sa.Column("resolved_at", UTCDateTime()),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_catalog_conflicts_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["catalog_products.id"],
                                name="fk_catalog_conflicts_product", ondelete="CASCADE"),
        sa.CheckConstraint("status IN ('pending', 'resolved', 'dismissed')",
                           name="ck_catalog_conflicts_status"),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_catalog_conflicts_scope_id"),
    )
    op.create_index("ix_catalog_conflicts_scope_status", "catalog_conflicts",
                    ["organization_id", "store_id", "status"])


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("catalog_conflicts", "catalog_extensions", "catalog_snapshots", "catalog_products"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("catalog downgrade refused: table contains data")
    op.drop_index("ix_catalog_conflicts_scope_status", table_name="catalog_conflicts")
    op.drop_table("catalog_conflicts")
    op.drop_table("catalog_extensions")
    op.drop_index("ix_catalog_snapshots_product", table_name="catalog_snapshots")
    op.drop_table("catalog_snapshots")
    op.drop_index("ix_catalog_products_scope", table_name="catalog_products")
    op.drop_table("catalog_products")
