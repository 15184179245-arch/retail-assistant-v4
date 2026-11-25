"""Sales draft, lines and handoff intent (SALE-002).

Revision ID: 0017_sales_draft
Revises: 0016_pricing_quote_policy
Forward-only; no business rows or defaults inserted. Downgrade refused while any
sales table contains data.
"""
from alembic import op
import sqlalchemy as sa

from backend.app.modules.agents.models import UTCDateTime

revision = "0017_sales_draft"
down_revision = "0016_pricing_quote_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sales_drafts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("creator_id", sa.String(36), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("quote_id", sa.String(36), nullable=True),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("sync_state", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("total_amount", sa.String(40), nullable=False),
        sa.Column("voided_at", UTCDateTime(), nullable=True),
        sa.Column("expires_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                                name="fk_sales_drafts_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "creator_id"], ["subjects.organization_id", "subjects.id"],
                                name="fk_sales_drafts_creator_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "quote_id"],
                                ["pricing_quotes.organization_id", "pricing_quotes.store_id", "pricing_quotes.id"],
                                name="fk_sales_drafts_quote_scope", ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_sales_drafts_scope_id"),
        sa.UniqueConstraint("organization_id", "store_id", "idempotency_key", name="uq_sales_drafts_key"),
        sa.CheckConstraint("stage IN ('draft', 'ready')", name="ck_sales_drafts_stage"),
        sa.CheckConstraint("status IN ('pending', 'paid')", name="ck_sales_drafts_status"),
        sa.CheckConstraint("sync_state IN ('local_only', 'pending', 'synced', 'conflict')",
                          name="ck_sales_drafts_sync_state"),
        sa.CheckConstraint("version > 0", name="ck_sales_drafts_version_positive"),
        sa.CheckConstraint("length(currency) = 3 AND currency = upper(currency)", name="ck_sales_drafts_currency"),
        sa.CheckConstraint("length(total_amount) BETWEEN 1 AND 40", name="ck_sales_drafts_total"),
    )
    op.create_index("ix_sales_drafts_scope_time", "sales_drafts",
                    ["organization_id", "store_id", "created_at", "id"])

    op.create_table(
        "sales_draft_lines",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("draft_id", sa.String(36), nullable=False),
        sa.Column("line_key", sa.String(200), nullable=False),
        sa.Column("product_id", sa.String(200), nullable=False),
        sa.Column("product_name", sa.String(200), nullable=False),
        sa.Column("quantity", sa.String(40), nullable=False),
        sa.Column("unit_price", sa.String(40), nullable=False),
        sa.Column("discount_bps", sa.Integer(), nullable=False),
        sa.Column("line_amount", sa.String(40), nullable=False),
        sa.Column("confirmed_by", sa.String(36), nullable=True),
        sa.Column("confirmed_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                                name="fk_sales_draft_lines_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "draft_id"],
                                ["sales_drafts.organization_id", "sales_drafts.store_id", "sales_drafts.id"],
                                name="fk_sales_draft_lines_draft_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "confirmed_by"],
                                ["subjects.organization_id", "subjects.id"],
                                name="fk_sales_draft_lines_confirmer_scope", ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_sales_draft_lines_scope_id"),
        sa.UniqueConstraint("organization_id", "store_id", "draft_id", "line_key",
                           name="uq_sales_draft_lines_draft_key"),
        sa.CheckConstraint("discount_bps BETWEEN 0 AND 10000", name="ck_sales_draft_lines_discount"),
        sa.CheckConstraint("length(quantity) BETWEEN 1 AND 40 AND length(unit_price) BETWEEN 1 AND 40 "
                           "AND length(line_amount) BETWEEN 1 AND 40", name="ck_sales_draft_lines_money"),
        sa.CheckConstraint("(confirmed_by IS NULL AND confirmed_at IS NULL) OR "
                           "(confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",
                           name="ck_sales_draft_lines_confirmation"),
    )
    op.create_index("ix_sales_draft_lines_draft", "sales_draft_lines",
                    ["organization_id", "store_id", "draft_id"])

    op.create_table(
        "sales_handoffs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("draft_id", sa.String(36), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("handoff_state", sa.String(24), nullable=False),
        sa.Column("external_ref", sa.String(200), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                                name="fk_sales_handoffs_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "draft_id"],
                                ["sales_drafts.organization_id", "sales_drafts.store_id", "sales_drafts.id"],
                                name="fk_sales_handoffs_draft_scope", ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_sales_handoffs_scope_id"),
        sa.UniqueConstraint("organization_id", "store_id", "idempotency_key", name="uq_sales_handoffs_key"),
        sa.UniqueConstraint("organization_id", "store_id", "draft_id", name="uq_sales_handoffs_one_per_draft"),
        sa.UniqueConstraint("organization_id", "store_id", "external_ref", name="uq_sales_handoffs_external_ref"),
        sa.CheckConstraint("handoff_state IN ('requested', 'sent', 'receipt_unknown', 'confirmed', 'failed')",
                           name="ck_sales_handoffs_state"),
        sa.CheckConstraint("length(external_ref) BETWEEN 1 AND 200", name="ck_sales_handoffs_ref"),
    )
    op.create_index("ix_sales_handoffs_draft", "sales_handoffs",
                    ["organization_id", "store_id", "draft_id"])

    op.create_table(
        "sales_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("handoff_id", sa.String(36), nullable=False),
        sa.Column("receipt_key", sa.String(200), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("external_ref", sa.String(200), nullable=False),
        sa.Column("paid", sa.Boolean(), nullable=False),
        sa.Column("unknown", sa.Boolean(), nullable=False),
        sa.Column("state_after", sa.String(24), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                                name="fk_sales_receipts_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "handoff_id"],
                                ["sales_handoffs.organization_id", "sales_handoffs.store_id", "sales_handoffs.id"],
                                name="fk_sales_receipts_handoff_scope", ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_sales_receipts_scope_id"),
        sa.UniqueConstraint("organization_id", "store_id", "receipt_key", name="uq_sales_receipts_key"),
        sa.CheckConstraint("state_after IN ('sent', 'receipt_unknown', 'confirmed')", name="ck_sales_receipts_state"),
        sa.CheckConstraint("length(receipt_key) BETWEEN 1 AND 200 AND length(digest) = 64",
                           name="ck_sales_receipts_identity"),
    )
    op.create_index("ix_sales_receipts_handoff", "sales_receipts",
                    ["organization_id", "store_id", "handoff_id"])


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("sales_receipts", "sales_handoffs", "sales_draft_lines", "sales_drafts"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("sales draft downgrade refused: table contains data")
    op.drop_index("ix_sales_receipts_handoff", table_name="sales_receipts")
    op.drop_table("sales_receipts")
    op.drop_index("ix_sales_handoffs_draft", table_name="sales_handoffs")
    op.drop_table("sales_handoffs")
    op.drop_index("ix_sales_draft_lines_draft", table_name="sales_draft_lines")
    op.drop_table("sales_draft_lines")
    op.drop_index("ix_sales_drafts_scope_time", table_name="sales_drafts")
    op.drop_table("sales_drafts")
