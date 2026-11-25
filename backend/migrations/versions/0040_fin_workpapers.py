"""FIN-001 immutable workpaper. Revision 0040_fin_workpapers after 0039."""
from alembic import op
import sqlalchemy as sa
from backend.app.modules.agents.models import UTCDateTime

revision = "0040_fin_workpapers"
down_revision = "0039_office_documents"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("finance_workpapers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("workpaper_key", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.String(36)),
        sa.Column("evidence_id", sa.String(36), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("request_key", sa.String(200), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("contents", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_fin_workpaper_scope"),
        sa.UniqueConstraint("organization_id", "store_id", "workpaper_key", "version", name="uq_fin_workpaper_version"),
        sa.UniqueConstraint("organization_id", "store_id", "request_key", name="uq_fin_workpaper_request"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_fin_workpaper_store", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "parent_id"], ["finance_workpapers.organization_id", "finance_workpapers.store_id", "finance_workpapers.id"], name="fk_fin_workpaper_parent", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "evidence_id"], ["office_evidence.organization_id", "office_evidence.store_id", "office_evidence.id"], name="fk_fin_workpaper_evidence", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "created_by"], ["subjects.organization_id", "subjects.id"], name="fk_fin_workpaper_author", ondelete="RESTRICT"),
        sa.CheckConstraint("version > 0 AND ((version=1 AND parent_id IS NULL) OR (version>1 AND parent_id IS NOT NULL))", name="ck_fin_workpaper_version"),
        sa.CheckConstraint("length(evidence_sha256)=64 AND length(request_digest)=64 AND length(content_sha256)=64", name="ck_fin_workpaper_digests"))
    op.create_index("ix_fin_workpaper_scope_created", "finance_workpapers", ["organization_id", "store_id", "created_at", "id"])
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(sa.text(f"CREATE TRIGGER finance_workpapers_no_{action.lower()} BEFORE {action} ON finance_workpapers BEGIN SELECT RAISE(ABORT,'finance workpaper immutable'); END"))
    elif dialect == "postgresql":
        op.execute(sa.text("CREATE FUNCTION deny_fin_workpaper_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'finance workpaper immutable'; END; $$"))
        op.execute(sa.text("CREATE TRIGGER finance_workpapers_immutable BEFORE UPDATE OR DELETE ON finance_workpapers FOR EACH ROW EXECUTE FUNCTION deny_fin_workpaper_mutation()"))

def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM finance_workpapers LIMIT 1")).first():
        raise RuntimeError("0040 downgrade refused with finance workpaper history; forward-fix")
    op.drop_index("ix_fin_workpaper_scope_created", table_name="finance_workpapers")
    op.drop_table("finance_workpapers")
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("DROP FUNCTION deny_fin_workpaper_mutation()"))
