"""PUR-001: employee-authorized evidence and manual pending-arrival intents."""
from alembic import op
import sqlalchemy as sa
from backend.app.modules.agents.models import UTCDateTime

revision = "0025_purchase_intents"
down_revision = "0024_search_read_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("purchase_evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("supplier_name", sa.String(200), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("consent_confirmed", sa.Boolean(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_purchase_evidence_scope_id"),
        sa.UniqueConstraint("organization_id", "store_id", "content_sha256", name="uq_purchase_evidence_digest"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                                name="fk_purchase_evidence_store", ondelete="RESTRICT"),
        sa.CheckConstraint("source_kind IN ('wechat_paste','delivery_note_text')", name="ck_purchase_evidence_source"),
        sa.CheckConstraint("length(original_text) BETWEEN 1 AND 10000", name="ck_purchase_evidence_text"),
        sa.CheckConstraint("length(supplier_name) BETWEEN 1 AND 200", name="ck_purchase_evidence_supplier"),
        sa.CheckConstraint("length(content_sha256) = 64", name="ck_purchase_evidence_sha"),
        sa.CheckConstraint("consent_confirmed = true", name="ck_purchase_evidence_consent"),
    )
    op.create_table("purchase_intents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("evidence_id", sa.String(36), nullable=False),
        sa.Column("creator_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_purchase_intents_scope_id"),
        sa.UniqueConstraint("organization_id", "store_id", "evidence_id", name="uq_purchase_intents_evidence"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "evidence_id"],
                                ["purchase_evidence.organization_id", "purchase_evidence.store_id", "purchase_evidence.id"],
                                name="fk_purchase_intents_evidence", ondelete="RESTRICT"),
        sa.CheckConstraint("status = 'awaiting_arrival'", name="ck_purchase_intents_status"),
        sa.CheckConstraint("version > 0", name="ck_purchase_intents_version"),
    )
    op.create_index("ix_purchase_intents_queue", "purchase_intents", ["organization_id", "store_id", "created_at"])
    op.create_table("purchase_intent_lines",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("intent_id", sa.String(36), nullable=False),
        sa.Column("original_description", sa.String(500), nullable=False),
        sa.Column("product_id", sa.String(36), nullable=True),
        sa.Column("product_version", sa.Integer(), nullable=True),
        sa.Column("quantity", sa.String(32), nullable=True),
        sa.Column("confirmed_by", sa.String(36), nullable=True),
        sa.Column("confirmed_at", UTCDateTime(), nullable=True),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_purchase_lines_scope_id"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "intent_id"],
                                ["purchase_intents.organization_id", "purchase_intents.store_id", "purchase_intents.id"],
                                name="fk_purchase_lines_intent", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "product_id"],
                                ["catalog_products.organization_id", "catalog_products.store_id", "catalog_products.id"],
                                name="fk_purchase_lines_product", ondelete="RESTRICT"),
        sa.CheckConstraint("length(original_description) BETWEEN 1 AND 500", name="ck_purchase_lines_description"),
        sa.CheckConstraint("(product_id IS NULL AND product_version IS NULL AND quantity IS NULL AND "
                           "confirmed_by IS NULL AND confirmed_at IS NULL) OR "
                           "(product_id IS NOT NULL AND product_version > 0 AND quantity IS NOT NULL AND "
                           "confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",
                           name="ck_purchase_lines_confirmed"),
    )
    op.create_index("ix_purchase_lines_intent", "purchase_intent_lines",
                    ["organization_id", "store_id", "intent_id"])
    op.create_table("purchase_commands",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "actor_id", "operation", "idempotency_key",
                            name="uq_purchase_commands_identity"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                                name="fk_purchase_commands_store", ondelete="RESTRICT"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("purchase_commands", "purchase_intent_lines", "purchase_intents", "purchase_evidence"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("0025 purchase downgrade refused: preserve evidence and use safe forward fix")
    op.drop_table("purchase_commands")
    op.drop_index("ix_purchase_lines_intent", table_name="purchase_intent_lines")
    op.drop_table("purchase_intent_lines")
    op.drop_index("ix_purchase_intents_queue", table_name="purchase_intents")
    op.drop_table("purchase_intents")
    op.drop_table("purchase_evidence")
