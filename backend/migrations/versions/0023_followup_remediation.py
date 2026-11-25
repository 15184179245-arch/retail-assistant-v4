"""Atomic memory receipts, sales quote binding, durable POS intent, exact lookup indexes.

Legacy sessionless personal memories remain stored but invisible. Forward-only
once new fields are used; preserve evidence rather than discard command history.
"""
from hashlib import sha256
import json
from alembic import op
import sqlalchemy as sa

revision = "0023_followup_remediation"
down_revision = "0022_memory_command_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("personal_memories", sa.Column("origin_session_id", sa.String(36), nullable=True))
    op.create_table("memory_commands",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "actor_id", "operation", "idempotency_key",
                            name="uq_memory_commands_identity"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                                name="fk_memory_commands_scope", ondelete="RESTRICT"))
    with op.batch_alter_table("sales_draft_lines") as batch:
        batch.add_column(sa.Column("quote_id", sa.String(36), nullable=True))
        batch.create_foreign_key("fk_sales_lines_quote_scope", "pricing_quotes",
            ["organization_id", "store_id", "quote_id"], ["organization_id", "store_id", "id"], ondelete="RESTRICT")
    op.create_table("pos_dispatches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("draft_id", sa.String(36), nullable=False),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("amount", sa.String(40), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("delivery_ref", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('dispatching', 'delivered', 'unknown')", name="ck_pos_dispatch_state"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "id"],
            ["sales_handoffs.organization_id", "sales_handoffs.store_id", "sales_handoffs.id"],
            name="fk_pos_dispatch_handoff_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "draft_id"],
            ["sales_drafts.organization_id", "sales_drafts.store_id", "sales_drafts.id"],
            name="fk_pos_dispatch_draft_scope", ondelete="RESTRICT"))
    op.add_column("catalog_products", sa.Column("barcode_sha256", sa.String(64), nullable=True))
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT p.id, s.payload FROM catalog_products p JOIN catalog_snapshots s ON s.id = p.current_snapshot_id"))
    # Stream batches: no full-catalog materialization or upstream payload mutation.
    for batch in iter(lambda: rows.fetchmany(500), []):
        for product_id, payload in batch:
            if isinstance(payload, str):
                payload = json.loads(payload)
            barcode = (payload or {}).get("barCode")
            if isinstance(barcode, str):
                connection.execute(sa.text("UPDATE catalog_products SET barcode_sha256=:digest WHERE id=:id"),
                    {"id": product_id, "digest": sha256(barcode.encode()).hexdigest()})
    op.create_index("ix_catalog_products_scope_barcode", "catalog_products", ["organization_id", "store_id", "active", "barcode_sha256"])
    op.create_index("ix_catalog_products_scope_code", "catalog_products", ["organization_id", "store_id", "active", "external_code"])


def downgrade():
    connection = op.get_bind()
    for query in (
        "SELECT COUNT(*) FROM memory_commands", "SELECT COUNT(*) FROM pos_dispatches",
        "SELECT COUNT(*) FROM personal_memories WHERE origin_session_id IS NOT NULL",
        "SELECT COUNT(*) FROM sales_draft_lines WHERE quote_id IS NOT NULL",
        "SELECT COUNT(*) FROM catalog_products WHERE barcode_sha256 IS NOT NULL",
    ):
        if connection.execute(sa.text(query)).scalar():
            raise RuntimeError("0023 downgrade refused: remediation data exists; use safe forward migration")
    op.drop_index("ix_catalog_products_scope_code", table_name="catalog_products")
    op.drop_index("ix_catalog_products_scope_barcode", table_name="catalog_products")
    op.drop_column("catalog_products", "barcode_sha256")
    op.drop_table("pos_dispatches")
    with op.batch_alter_table("sales_draft_lines") as batch:
        batch.drop_constraint("fk_sales_lines_quote_scope", type_="foreignkey")
        batch.drop_column("quote_id")
    op.drop_table("memory_commands")
    op.drop_column("personal_memories", "origin_session_id")
