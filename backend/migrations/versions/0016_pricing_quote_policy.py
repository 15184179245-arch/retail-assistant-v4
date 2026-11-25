"""Price source rules, immutable quote snapshots and approval bindings (SALE-001).

Revision ID: 0016_pricing_quote_policy
Revises: 0015_agent_cost_ledger

Forward-only: no default rules, prices, currencies or business rows are inserted.
Downgrade is refused while any new table contains data, preserving quote and
approval evidence.
"""
from alembic import op
import sqlalchemy as sa

from backend.app.modules.agents.models import UTCDateTime

revision = "0016_pricing_quote_policy"
down_revision = "0015_agent_cost_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pricing_rules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("rule_key", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount_precision", sa.Integer(), nullable=False),
        sa.Column("rounding", sa.String(16), nullable=False),
        sa.Column("price_source_types", sa.JSON(), nullable=False),
        sa.Column("max_discount_bps", sa.Integer(), nullable=False),
        sa.Column("approval_threshold_bps", sa.Integer(), nullable=False),
        sa.Column("source_max_age_seconds", sa.Integer(), nullable=False),
        sa.Column("quote_ttl_seconds", sa.Integer(), nullable=False),
        sa.Column("effective_from", UTCDateTime(), nullable=False),
        sa.Column("effective_until", UTCDateTime(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_pricing_rules_store_scope", ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_pricing_rules_scope_id"),
        sa.UniqueConstraint("organization_id", "store_id", "rule_key", "version",
                           name="uq_pricing_rules_key_version"),
        sa.CheckConstraint("version > 0", name="ck_pricing_rules_version_positive"),
        sa.CheckConstraint("length(currency) = 3 AND currency = upper(currency)",
                          name="ck_pricing_rules_currency"),
        sa.CheckConstraint("amount_precision BETWEEN 0 AND 6", name="ck_pricing_rules_precision"),
        sa.CheckConstraint("rounding IN ('ROUND_HALF_UP', 'ROUND_CEILING', 'ROUND_DOWN')",
                          name="ck_pricing_rules_rounding"),
        sa.CheckConstraint("max_discount_bps BETWEEN 0 AND 10000 AND approval_threshold_bps BETWEEN 0 AND max_discount_bps",
                          name="ck_pricing_rules_discount_bounds"),
        sa.CheckConstraint("source_max_age_seconds > 0 AND quote_ttl_seconds BETWEEN 60 AND 86400",
                          name="ck_pricing_rules_time_bounds"),
        sa.CheckConstraint("effective_until IS NULL OR effective_until > effective_from",
                          name="ck_pricing_rules_window"),
    )
    op.create_index("ix_pricing_rules_active", "pricing_rules",
                    ["organization_id", "store_id", "rule_key", "active", "effective_from"])

    op.create_table(
        "pricing_quotes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("rule_id", sa.String(36), nullable=False),
        sa.Column("rule_key", sa.String(120), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("quote_key", sa.String(200), nullable=False),
        sa.Column("requester_id", sa.String(36), nullable=False),
        sa.Column("object_type", sa.String(64), nullable=False),
        sa.Column("object_id", sa.String(200), nullable=False),
        sa.Column("object_version", sa.String(100), nullable=False),
        sa.Column("product_id", sa.String(200), nullable=False),
        sa.Column("quantity", sa.String(40), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount_precision", sa.Integer(), nullable=False),
        sa.Column("rounding", sa.String(16), nullable=False),
        sa.Column("list_price", sa.String(40), nullable=False),
        sa.Column("unit_price", sa.String(40), nullable=False),
        sa.Column("discount_bps", sa.Integer(), nullable=False),
        sa.Column("final_amount", sa.String(40), nullable=False),
        sa.Column("price_source_type", sa.String(40), nullable=False),
        sa.Column("price_source_ref", sa.String(200), nullable=False),
        sa.Column("price_source_version", sa.String(120), nullable=False),
        sa.Column("price_source_snapshot_id", sa.String(36), nullable=False),
        sa.Column("price_source_payload_sha256", sa.String(64), nullable=False),
        sa.Column("source_observed_at", UTCDateTime(), nullable=False),
        sa.Column("source_expires_at", UTCDateTime(), nullable=True),
        sa.Column("cost_amount", sa.String(40), nullable=True),
        sa.Column("cost_source_type", sa.String(40), nullable=True),
        sa.Column("cost_source_ref", sa.String(200), nullable=True),
        sa.Column("cost_observed_at", UTCDateTime(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("snapshot_digest", sa.String(64), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_pricing_quotes_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "rule_id"],
                                ["pricing_rules.organization_id", "pricing_rules.store_id", "pricing_rules.id"],
                                name="fk_pricing_quotes_rule_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "requester_id"],
                                ["subjects.organization_id", "subjects.id"],
                                name="fk_pricing_quotes_requester_scope", ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_pricing_quotes_scope_id"),
        sa.UniqueConstraint("organization_id", "store_id", "quote_key", name="uq_pricing_quotes_key"),
        sa.CheckConstraint("status IN ('quoted', 'approval_required')", name="ck_pricing_quotes_status"),
        sa.CheckConstraint("discount_bps BETWEEN 0 AND 10000", name="ck_pricing_quotes_discount"),
        sa.CheckConstraint("length(currency) = 3 AND currency = upper(currency)", name="ck_pricing_quotes_currency"),
        sa.CheckConstraint("amount_precision BETWEEN 0 AND 6", name="ck_pricing_quotes_precision"),
        sa.CheckConstraint("rounding IN ('ROUND_HALF_UP', 'ROUND_CEILING', 'ROUND_DOWN')",
                          name="ck_pricing_quotes_rounding"),
        sa.CheckConstraint("length(quote_key) > 0 AND length(object_type) > 0 AND length(object_id) > 0 "
                           "AND length(object_version) > 0 AND length(product_id) > 0",
                          name="ck_pricing_quotes_identity_nonempty"),
        sa.CheckConstraint("length(snapshot_digest) = 64", name="ck_pricing_quotes_digest"),
    )
    op.create_index("ix_pricing_quotes_scope_time", "pricing_quotes",
                    ["organization_id", "store_id", "created_at", "id"])
    op.create_index("ix_pricing_quotes_requester", "pricing_quotes",
                    ["organization_id", "store_id", "requester_id", "created_at"])

    op.create_table(
        "pricing_quote_approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("quote_id", sa.String(36), nullable=False),
        sa.Column("approval_id", sa.String(36), nullable=False),
        sa.Column("object_type", sa.String(64), nullable=False),
        sa.Column("object_id", sa.String(200), nullable=False),
        sa.Column("object_version", sa.String(100), nullable=False),
        sa.Column("snapshot_digest", sa.String(64), nullable=False),
        sa.Column("rule_id", sa.String(36), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("bound_by", sa.String(36), nullable=False),
        sa.Column("bound_at", UTCDateTime(), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("last_validated_at", UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_pricing_quote_approvals_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "quote_id"],
                                ["pricing_quotes.organization_id", "pricing_quotes.store_id", "pricing_quotes.id"],
                                name="fk_pricing_quote_approvals_quote", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "approval_id"],
                                ["approval_requests.organization_id", "approval_requests.store_id", "approval_requests.id"],
                                name="fk_pricing_quote_approvals_approval", ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_pricing_quote_approvals_scope_id"),
        sa.UniqueConstraint("organization_id", "store_id", "quote_id", name="uq_pricing_quote_approvals_quote"),
        sa.UniqueConstraint("organization_id", "store_id", "approval_id", name="uq_pricing_quote_approvals_approval"),
        sa.CheckConstraint("state IN ('bound', 'revoked')", name="ck_pricing_quote_approvals_state"),
        sa.CheckConstraint("length(snapshot_digest) = 64", name="ck_pricing_quote_approvals_digest"),
        sa.CheckConstraint("expires_at > bound_at", name="ck_pricing_quote_approvals_window"),
    )
    op.create_index("ix_pricing_quote_approvals_state", "pricing_quote_approvals",
                    ["organization_id", "store_id", "state", "expires_at"])
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            "CREATE TRIGGER pricing_rules_append_only_update BEFORE UPDATE ON pricing_rules "
            "BEGIN SELECT RAISE(ABORT, 'pricing rules are append-only'); END"
        )
        op.execute(
            "CREATE TRIGGER pricing_rules_append_only_delete BEFORE DELETE ON pricing_rules "
            "BEGIN SELECT RAISE(ABORT, 'pricing rules are append-only'); END"
        )
    elif dialect == "postgresql":
        op.execute(
            "CREATE OR REPLACE FUNCTION pricing_rules_append_only() RETURNS trigger "
            "AS $$ BEGIN RAISE EXCEPTION 'pricing rules are append-only'; END; "
            "$$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER pricing_rules_append_only_guard BEFORE UPDATE OR DELETE "
            "ON pricing_rules FOR EACH ROW EXECUTE FUNCTION pricing_rules_append_only()"
        )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("pricing_quote_approvals", "pricing_quotes", "pricing_rules"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("pricing downgrade refused: table contains data")
    op.drop_index("ix_pricing_quote_approvals_state", table_name="pricing_quote_approvals")
    op.drop_table("pricing_quote_approvals")
    op.drop_index("ix_pricing_quotes_requester", table_name="pricing_quotes")
    op.drop_index("ix_pricing_quotes_scope_time", table_name="pricing_quotes")
    op.drop_table("pricing_quotes")
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS pricing_rules_append_only_update")
        op.execute("DROP TRIGGER IF EXISTS pricing_rules_append_only_delete")
    elif bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS pricing_rules_append_only_guard ON pricing_rules")
        op.execute("DROP FUNCTION IF EXISTS pricing_rules_append_only()")
    op.drop_index("ix_pricing_rules_active", table_name="pricing_rules")
    op.drop_table("pricing_rules")
