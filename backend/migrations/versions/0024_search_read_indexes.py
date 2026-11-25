"""Read-only search hot path over rebuildable derived data; source rows untouched."""
from hashlib import sha256
from alembic import op
import sqlalchemy as sa

revision = "0024_search_read_indexes"
down_revision = "0023_followup_remediation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("search_projections", sa.Column("normalized_barcode_sha256", sa.String(64), nullable=True))
    op.add_column("search_projections", sa.Column("search_text", sa.Text(), nullable=False, server_default=""))
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT product_id, name_text, alias_text, pinyin_text, barcode FROM search_projections")).all()
    for identity, name, alias, pinyin, barcode in rows:
        normalized = barcode.strip() if isinstance(barcode, str) else None
        if normalized and len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in "\"'":
            normalized = normalized[1:-1]
        connection.execute(sa.text("UPDATE search_projections SET search_text=:text, normalized_barcode_sha256=:hash, snapshot_id=NULL WHERE product_id=:id"),
            {"id": identity, "text": "\n".join((name, alias, pinyin)).casefold(),
             "hash": sha256(normalized.encode()).hexdigest() if normalized else None})
    # Existing projections are explicitly stale until rebuilt with source pyCode.
    op.create_index("ix_search_projections_normalized", "search_projections",
                    ["organization_id", "store_id", "normalized_barcode_sha256"])
    if connection.dialect.name == "sqlite":
        op.execute("CREATE VIRTUAL TABLE search_projection_fts USING fts5(product_id UNINDEXED, organization_id UNINDEXED, store_id UNINDEXED, search_text, content='search_projections', content_rowid='rowid', tokenize='trigram')")
        op.execute("CREATE TRIGGER search_projection_fts_ai AFTER INSERT ON search_projections BEGIN INSERT INTO search_projection_fts(rowid,product_id,organization_id,store_id,search_text) VALUES(new.rowid,new.product_id,new.organization_id,new.store_id,new.search_text); END")
        op.execute("CREATE TRIGGER search_projection_fts_ad AFTER DELETE ON search_projections BEGIN INSERT INTO search_projection_fts(search_projection_fts,rowid,product_id,organization_id,store_id,search_text) VALUES('delete',old.rowid,old.product_id,old.organization_id,old.store_id,old.search_text); END")
        op.execute("CREATE TRIGGER search_projection_fts_au AFTER UPDATE ON search_projections BEGIN INSERT INTO search_projection_fts(search_projection_fts,rowid,product_id,organization_id,store_id,search_text) VALUES('delete',old.rowid,old.product_id,old.organization_id,old.store_id,old.search_text); INSERT INTO search_projection_fts(rowid,product_id,organization_id,store_id,search_text) VALUES(new.rowid,new.product_id,new.organization_id,new.store_id,new.search_text); END")
        op.execute("INSERT INTO search_projection_fts(search_projection_fts) VALUES('rebuild')")


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT COUNT(*) FROM search_projections")).scalar():
        raise RuntimeError("0024 downgrade refused while derived projections exist; retain data and forward-fix")
    if connection.dialect.name == "sqlite":
        for name in ("ai", "ad", "au"):
            op.execute(f"DROP TRIGGER search_projection_fts_{name}")
        op.execute("DROP TABLE search_projection_fts")
    op.drop_index("ix_search_projections_normalized", table_name="search_projections")
    op.drop_column("search_projections", "search_text")
    op.drop_column("search_projections", "normalized_barcode_sha256")
