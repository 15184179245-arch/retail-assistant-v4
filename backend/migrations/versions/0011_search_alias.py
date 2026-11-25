"""Search aliases and rebuildable projection (SRCH-002).

Revision ID: 0011_search_alias
Revises: 0010_catalog
"""

from alembic import op
import sqlalchemy as sa

from backend.app.modules.agents.models import UTCDateTime

revision = "0011_search_alias"
down_revision = "0010_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_aliases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("product_id", sa.String(36), nullable=False),
        sa.Column("alias_value", sa.String(120), nullable=False),
        sa.Column("alias_kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("revoked_by", sa.String(36)),
        sa.Column("revoked_at", UTCDateTime()),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_product_aliases_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["catalog_products.id"],
                                name="fk_product_aliases_product", ondelete="CASCADE"),
        sa.CheckConstraint("alias_kind IN ('code', 'name', 'pinyin')",
                           name="ck_product_aliases_kind"),
        sa.CheckConstraint("status IN ('active', 'revoked')",
                           name="ck_product_aliases_status"),
        sa.UniqueConstraint("organization_id", "store_id", "product_id", "alias_kind",
                            "alias_value", name="uq_product_aliases_binding"),
        sa.UniqueConstraint("organization_id", "store_id", "id",
                            name="uq_product_aliases_scope_id"),
    )
    op.create_index("ix_product_aliases_lookup", "product_aliases",
                    ["organization_id", "store_id", "alias_value", "status"])
    op.create_table(
        "search_projections",
        sa.Column("product_id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("code", sa.String(120), nullable=False),
        sa.Column("barcode", sa.String(200)),
        sa.Column("snapshot_id", sa.String(36)),
        sa.Column("name_text", sa.String(500), nullable=False),
        sa.Column("alias_text", sa.String(2000), nullable=False),
        sa.Column("pinyin_text", sa.String(2000), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_search_projections_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["catalog_products.id"],
                                name="fk_search_projections_product", ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", "store_id", "product_id",
                            name="uq_search_projections_scope_product"),
    )
    op.create_index("ix_search_projections_scope", "search_projections",
                    ["organization_id", "store_id"])


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("product_aliases", "search_projections"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("search alias downgrade refused: table contains data")
    op.drop_index("ix_search_projections_scope", table_name="search_projections")
    op.drop_table("search_projections")
    op.drop_index("ix_product_aliases_lookup", table_name="product_aliases")
    op.drop_table("product_aliases")
