"""Progressive locations and append-only placement history (STK-002)."""
from alembic import op
import sqlalchemy as sa

from backend.app.modules.agents.models import UTCDateTime

revision = "0020_locations"
down_revision = "0019_media_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "locations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("parent_id", sa.String(36), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_locations_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "parent_id"],
                                ["locations.organization_id", "locations.store_id", "locations.id"],
                                name="fk_locations_parent_scope", ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_locations_scope_id"),
        sa.CheckConstraint("kind IN ('area','temporary','unknown','rack','shelf','bin')",
                           name="ck_locations_kind"),
    )
    op.create_index("ix_locations_scope_parent", "locations",
                    ["organization_id", "store_id", "parent_id"])
    op.create_table(
        "placement_assertions",
        sa.Column("event_seq", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("product_id", sa.String(36), nullable=False),
        sa.Column("location_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("asserted_by", sa.String(36), nullable=False),
        sa.Column("asserted_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_placement_assertions_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "product_id"],
                                ["catalog_products.organization_id", "catalog_products.store_id",
                                 "catalog_products.id"],
                                name="fk_placement_assertions_product_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "location_id"],
                                ["locations.organization_id", "locations.store_id", "locations.id"],
                                name="fk_placement_assertions_location_scope", ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "store_id", "id",
                            name="uq_placement_assertions_scope_id"),
        sa.UniqueConstraint("organization_id", "store_id", "idempotency_key",
                            name="uq_placement_assertions_idem"),
        sa.CheckConstraint("kind IN ('present','ended')", name="ck_placement_assertions_kind"),
    )
    op.create_index("ix_placement_assertions_product", "placement_assertions",
                    ["organization_id", "store_id", "product_id"])
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            "CREATE TRIGGER placement_assertions_append_only_update "
            "BEFORE UPDATE ON placement_assertions "
            "BEGIN SELECT RAISE(ABORT, 'placement assertions are append-only'); END"
        )
        op.execute(
            "CREATE TRIGGER placement_assertions_append_only_delete "
            "BEFORE DELETE ON placement_assertions "
            "BEGIN SELECT RAISE(ABORT, 'placement assertions are append-only'); END"
        )
    elif dialect == "postgresql":
        op.execute(
            "CREATE OR REPLACE FUNCTION placement_assertions_append_only() RETURNS trigger "
            "AS $$ BEGIN RAISE EXCEPTION 'placement assertions are append-only'; END; "
            "$$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER placement_assertions_append_only_guard "
            "BEFORE UPDATE OR DELETE ON placement_assertions FOR EACH ROW "
            "EXECUTE FUNCTION placement_assertions_append_only()"
        )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT COUNT(*) FROM placement_assertions")).scalar() or \
            connection.execute(sa.text("SELECT COUNT(*) FROM locations")).scalar():
        raise RuntimeError("locations downgrade refused while rows exist")
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS placement_assertions_append_only_update")
        op.execute("DROP TRIGGER IF EXISTS placement_assertions_append_only_delete")
    elif bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS placement_assertions_append_only_guard ON placement_assertions")
        op.execute("DROP FUNCTION IF EXISTS placement_assertions_append_only()")
    op.drop_index("ix_placement_assertions_product", table_name="placement_assertions")
    op.drop_table("placement_assertions")
    op.drop_index("ix_locations_scope_parent", table_name="locations")
    op.drop_table("locations")
