"""STK-003 append-only scoped partial observations; no ERP inventory writes."""
from alembic import op
import sqlalchemy as sa
from backend.app.modules.agents.models import UTCDateTime

revision = "0026_stocktake_observations"
down_revision = "0025_purchase_intents"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "stocktake_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("opened_by", sa.String(36), nullable=False),
        sa.Column("coverage_kind", sa.String(20), nullable=False),
        sa.Column("coverage_location_id", sa.String(36), nullable=True),
        sa.Column("coverage_note", sa.String(500), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "id",
                            name="uq_stocktake_sessions_scope"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_stocktake_sessions_store", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "coverage_location_id"],
                                ["locations.organization_id", "locations.store_id", "locations.id"],
                                name="fk_stocktake_sessions_coverage", ondelete="RESTRICT"),
        sa.CheckConstraint("coverage_kind IN ('partial_area','partial_shelf','unknown')",
                           name="ck_stocktake_sessions_coverage_kind"),
        sa.CheckConstraint("status = 'open'", name="ck_stocktake_sessions_open"),
        sa.CheckConstraint("version > 0", name="ck_stocktake_sessions_version"),
        sa.CheckConstraint("length(coverage_note) BETWEEN 1 AND 500",
                           name="ck_stocktake_sessions_note"),
    )
    op.create_table(
        "stocktake_observations",
        sa.Column("event_seq", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("supersedes_id", sa.String(36), nullable=True),
        sa.Column("product_id", sa.String(36), nullable=True),
        sa.Column("unknown_product_label", sa.String(200), nullable=True),
        sa.Column("location_id", sa.String(36), nullable=True),
        sa.Column("unknown_location_label", sa.String(200), nullable=True),
        sa.Column("quantity", sa.String(40), nullable=False),
        sa.Column("unit", sa.String(24), nullable=False),
        sa.Column("media_id", sa.String(36), nullable=True),
        sa.Column("observed_by", sa.String(36), nullable=False),
        sa.Column("observed_at", UTCDateTime(), nullable=False),
        sa.Column("recorded_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "id",
                            name="uq_stocktake_observations_scope"),
        sa.UniqueConstraint("organization_id", "store_id", "session_id", "id",
                            name="uq_stocktake_observations_session_id"),
        sa.UniqueConstraint("organization_id", "store_id", "session_id", "supersedes_id",
                            name="uq_stocktake_observations_single_correction"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "session_id"],
                                ["stocktake_sessions.organization_id",
                                 "stocktake_sessions.store_id", "stocktake_sessions.id"],
                                name="fk_stocktake_observations_session", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "product_id"],
                                ["catalog_products.organization_id", "catalog_products.store_id",
                                 "catalog_products.id"],
                                name="fk_stocktake_observations_product", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "location_id"],
                                ["locations.organization_id", "locations.store_id", "locations.id"],
                                name="fk_stocktake_observations_location", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "media_id"],
                                ["media_assets.organization_id", "media_assets.store_id",
                                 "media_assets.id"],
                                name="fk_stocktake_observations_media", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["organization_id", "store_id", "session_id", "supersedes_id"],
            ["stocktake_observations.organization_id", "stocktake_observations.store_id",
             "stocktake_observations.session_id", "stocktake_observations.id"],
            name="fk_stocktake_observations_prior", ondelete="RESTRICT"),
        sa.CheckConstraint("kind IN ('observed','correction')",
                           name="ck_stocktake_observations_kind"),
        sa.CheckConstraint("(kind = 'observed' AND supersedes_id IS NULL) OR "
                           "(kind = 'correction' AND supersedes_id IS NOT NULL)",
                           name="ck_stocktake_observations_correction"),
        sa.CheckConstraint("(product_id IS NOT NULL AND unknown_product_label IS NULL) OR "
                           "(product_id IS NULL AND unknown_product_label IS NOT NULL)",
                           name="ck_stocktake_observations_product_identity"),
        sa.CheckConstraint("(location_id IS NOT NULL AND unknown_location_label IS NULL) OR "
                           "(location_id IS NULL AND unknown_location_label IS NOT NULL)",
                           name="ck_stocktake_observations_location_identity"),
    )
    op.create_index("ix_stocktake_observations_scope_seq", "stocktake_observations",
                    ["organization_id", "store_id", "session_id", "event_seq"])
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER stocktake_observations_block_{action.lower()} "
                f"BEFORE {action} ON stocktake_observations BEGIN "
                "SELECT RAISE(ABORT,'stocktake observations append-only'); END"
            )
    elif connection.dialect.name == "postgresql":
        op.execute(
            "CREATE OR REPLACE FUNCTION stocktake_observations_immutable() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'stocktake observations append-only'; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER stocktake_observations_immutable_guard "
            "BEFORE UPDATE OR DELETE ON stocktake_observations FOR EACH ROW "
            "EXECUTE FUNCTION stocktake_observations_immutable()"
        )
    op.create_table(
        "stocktake_commands",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "actor_id", "operation",
                            "idempotency_key", name="uq_stocktake_commands_identity"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_stocktake_commands_store", ondelete="RESTRICT"),
    )


def downgrade():
    bind = op.get_bind()
    for table in ("stocktake_commands", "stocktake_observations", "stocktake_sessions"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("0026 stocktake downgrade refused with observations/commands; forward-fix")
    op.drop_table("stocktake_commands")
    op.drop_index("ix_stocktake_observations_scope_seq", table_name="stocktake_observations")
    op.drop_table("stocktake_observations")
    op.drop_table("stocktake_sessions")
    if bind.dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS stocktake_observations_immutable()")
