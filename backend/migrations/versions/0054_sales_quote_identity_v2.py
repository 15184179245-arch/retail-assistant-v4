"""SALE-005 additive v2 facts. No backfill, no destructive downgrade.

The new-write gate defaults off. Rollback disables SALES_QUOTE_V2_ENABLED and
keeps a compatible reader; deploy a safe forward fix, never erase v2/unknown.
This revision freezes its own DDL; it does not import runtime ORM models.
"""
from alembic import op
import sqlalchemy as sa

revision = "0054_sales_quote_identity_v2"
down_revision = "0053_knowledge_revision_access"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sales_draft_lines", sa.Column("catalog_product_id", sa.String(36), nullable=True))
    op.add_column("sales_draft_lines", sa.Column("unit", sa.String(80), nullable=True))
    op.add_column("sales_draft_lines", sa.Column("quote_v2_id", sa.String(36), nullable=True))
    op.create_table('pricing_source_currency_policies_v2',
        sa.Column('id', sa.String(36), nullable=False, primary_key=True),
        sa.Column('organization_id', sa.String(36), nullable=False, primary_key=False),
        sa.Column('store_id', sa.String(36), nullable=False, primary_key=False),
        sa.Column('source_system', sa.String(40), nullable=False, primary_key=False),
        sa.Column('policy_key', sa.String(120), nullable=False, primary_key=False),
        sa.Column('version', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('currency', sa.String(3), nullable=False, primary_key=False),
        sa.Column('state', sa.String(16), nullable=False, primary_key=False),
        sa.Column('evidence_ref', sa.String(500), nullable=False, primary_key=False),
        sa.Column('evidence_sha256', sa.String(64), nullable=False, primary_key=False),
        sa.Column('effective_from', sa.DateTime(timezone=True), nullable=False, primary_key=False),
        sa.Column('effective_until', sa.DateTime(timezone=True), nullable=False, primary_key=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, primary_key=False),
        sa.CheckConstraint("version > 0 AND state IN ('active', 'revoked')", name='ck_currency_v2_state'),
        sa.CheckConstraint('effective_until > effective_from', name='ck_currency_v2_window'),
        sa.CheckConstraint('length(currency) = 3 AND currency = upper(currency)', name='ck_currency_v2_currency'),
        sa.CheckConstraint('length(evidence_ref) > 0 AND length(evidence_sha256) = 64', name='ck_currency_v2_evidence'),
        sa.ForeignKeyConstraint(['organization_id', 'store_id'], ['stores.organization_id', 'stores.id'], ondelete="RESTRICT"),
        sa.UniqueConstraint('organization_id', 'store_id', 'source_system', 'policy_key', 'version', name='uq_currency_v2_version'),
    )
    op.create_table('pricing_quotes_v2',
        sa.Column('id', sa.String(36), nullable=False, primary_key=True),
        sa.Column('organization_id', sa.String(36), nullable=False, primary_key=False),
        sa.Column('store_id', sa.String(36), nullable=False, primary_key=False),
        sa.Column('draft_id', sa.String(36), nullable=False, primary_key=False),
        sa.Column('requester_id', sa.String(36), nullable=False, primary_key=False),
        sa.Column('quote_key', sa.String(200), nullable=False, primary_key=False),
        sa.Column('line_key', sa.String(200), nullable=False, primary_key=False),
        sa.Column('request_digest', sa.String(64), nullable=False, primary_key=False),
        sa.Column('snapshot_digest', sa.String(64), nullable=False, primary_key=False),
        sa.Column('request', sa.JSON(), nullable=False, primary_key=False),
        sa.Column('authority', sa.JSON(), nullable=False, primary_key=False),
        sa.Column('context', sa.JSON(), nullable=False, primary_key=False),
        sa.Column('response', sa.JSON(), nullable=False, primary_key=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, primary_key=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False, primary_key=False),
        sa.CheckConstraint('length(request_digest) = 64 AND length(snapshot_digest) = 64', name='ck_quote_v2_digests'),
        sa.ForeignKeyConstraint(['organization_id', 'store_id', 'draft_id'], ['sales_drafts.organization_id', 'sales_drafts.store_id', 'sales_drafts.id'], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['organization_id', 'requester_id'], ['subjects.organization_id', 'subjects.id'], ondelete="RESTRICT"),
        sa.UniqueConstraint('organization_id', 'store_id', 'id', name='uq_quote_v2_scope'),
        sa.UniqueConstraint('organization_id', 'store_id', 'quote_key', name='uq_quote_v2_key'),
    )
    op.create_table('sales_line_commands_v2',
        sa.Column('id', sa.String(36), nullable=False, primary_key=True),
        sa.Column('organization_id', sa.String(36), nullable=False, primary_key=False),
        sa.Column('store_id', sa.String(36), nullable=False, primary_key=False),
        sa.Column('draft_id', sa.String(36), nullable=False, primary_key=False),
        sa.Column('actor_id', sa.String(36), nullable=False, primary_key=False),
        sa.Column('line_key', sa.String(200), nullable=False, primary_key=False),
        sa.Column('quote_id', sa.String(36), nullable=False, primary_key=False),
        sa.Column('line_id', sa.String(36), nullable=False, primary_key=False),
        sa.Column('request_digest', sa.String(64), nullable=False, primary_key=False),
        sa.Column('response', sa.JSON(), nullable=False, primary_key=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, primary_key=False),
        sa.CheckConstraint('length(request_digest) = 64', name='ck_line_command_v2_digest'),
        sa.ForeignKeyConstraint(['organization_id', 'store_id', 'draft_id'], ['sales_drafts.organization_id', 'sales_drafts.store_id', 'sales_drafts.id'], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['organization_id', 'store_id', 'line_id'], ['sales_draft_lines.organization_id', 'sales_draft_lines.store_id', 'sales_draft_lines.id'], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['organization_id', 'store_id', 'quote_id'], ['pricing_quotes_v2.organization_id', 'pricing_quotes_v2.store_id', 'pricing_quotes_v2.id'], ondelete="RESTRICT"),
        sa.UniqueConstraint('organization_id', 'store_id', 'draft_id', 'line_key', name='uq_line_command_v2_key'),
    )
    connection = op.get_bind()
    for table in ("pricing_source_currency_policies_v2", "pricing_quotes_v2", "sales_line_commands_v2"):
        if connection.dialect.name == "sqlite":
            for operation in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER {table}_{operation.lower()}_immutable BEFORE {operation} ON {table} "
                           "BEGIN SELECT RAISE(ABORT, 'v2 facts are append-only'); END")
        elif connection.dialect.name == "postgresql":
            op.execute(f"CREATE FUNCTION {table}_immutable() RETURNS trigger AS $$ BEGIN "
                       "RAISE EXCEPTION 'v2 facts are append-only'; END; $$ LANGUAGE plpgsql")
            op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                       f"FOR EACH ROW EXECUTE FUNCTION {table}_immutable()")


def downgrade():
    raise RuntimeError("SALE-005 requires safe forward recovery: disable new v2 writes and preserve all facts")
