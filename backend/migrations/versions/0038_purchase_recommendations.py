"""ANA-005 additive immutable candidate and human decision receipts.

Revision ID: 0038_purchase_recommendations
Revises: 0037_erp_command_gateway
"""
from alembic import op
import sqlalchemy as sa
from backend.app.modules.agents.models import UTCDateTime

revision = "0038_purchase_recommendations"
down_revision = "0037_erp_command_gateway"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "purchase_recommendations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("product_id", sa.String(36), nullable=False),
        sa.Column("product_version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("request_key", sa.String(120), nullable=False),
        sa.Column("formula_version", sa.String(60), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("candidate_digest", sa.String(64), nullable=False),
        sa.Column("source_evidence", sa.JSON(), nullable=False),
        sa.Column("projection", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "id",
                            name="uq_purchase_recommendation_scope"),
        sa.UniqueConstraint("organization_id", "store_id", "created_by", "request_key",
                            name="uq_purchase_recommendation_request"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
            ["stores.organization_id", "stores.id"],
            name="fk_purchase_recommendation_store", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "product_id"],
            ["catalog_products.organization_id", "catalog_products.store_id", "catalog_products.id"],
            name="fk_purchase_recommendation_product", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "created_by"],
            ["subjects.organization_id", "subjects.id"],
            name="fk_purchase_recommendation_creator", ondelete="RESTRICT"),
        sa.CheckConstraint("product_version > 0", name="ck_purchase_recommendation_version"),
        sa.CheckConstraint("length(source_digest)=64 AND length(candidate_digest)=64",
                           name="ck_purchase_recommendation_digests"),
    )
    op.create_index("ix_purchase_recommendation_scope_time", "purchase_recommendations",
                    ["organization_id", "store_id", "created_at", "id"])
    op.create_table(
        "purchase_recommendation_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("recommendation_id", sa.String(36), nullable=False),
        sa.Column("decided_by", sa.String(36), nullable=False),
        sa.Column("decision_key", sa.String(120), nullable=False),
        sa.Column("decision", sa.String(12), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("expected_candidate_digest", sa.String(64), nullable=False),
        sa.Column("quantity_pieces", sa.Integer()),
        sa.Column("local_draft", sa.JSON()),
        sa.Column("decided_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "recommendation_id",
                            name="uq_purchase_recommendation_decision_once"),
        sa.UniqueConstraint("organization_id", "store_id", "decided_by", "decision_key",
                            name="uq_purchase_recommendation_decision_key"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "recommendation_id"],
            ["purchase_recommendations.organization_id", "purchase_recommendations.store_id",
             "purchase_recommendations.id"],
            name="fk_purchase_recommendation_decision_parent", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "decided_by"],
            ["subjects.organization_id", "subjects.id"],
            name="fk_purchase_recommendation_reviewer", ondelete="RESTRICT"),
        sa.CheckConstraint("decision IN ('accept','modify','reject','defer')",
                           name="ck_purchase_recommendation_decision_choice"),
        sa.CheckConstraint("length(expected_candidate_digest)=64",
                           name="ck_purchase_recommendation_decision_digest"),
        sa.CheckConstraint("(decision IN ('accept','modify') AND quantity_pieces > 0 "
                           "AND local_draft IS NOT NULL) OR "
                           "(decision IN ('reject','defer') AND quantity_pieces IS NULL "
                           "AND local_draft IS NULL)",
                           name="ck_purchase_recommendation_decision_payload"),
    )
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for table in ("purchase_recommendations", "purchase_recommendation_decisions"):
            for action in ("UPDATE", "DELETE"):
                op.execute(sa.text(
                    f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT,'purchase recommendation evidence immutable'); END"))
    elif bind.dialect.name == "postgresql":
        op.execute(sa.text(
            "CREATE FUNCTION deny_purchase_recommendation_mutation() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'purchase recommendation evidence immutable'; END; $$"))
        for table in ("purchase_recommendations", "purchase_recommendation_decisions"):
            op.execute(sa.text(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION deny_purchase_recommendation_mutation()"))


def downgrade():
    bind = op.get_bind()
    for table in ("purchase_recommendation_decisions", "purchase_recommendations"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("0038 downgrade refused with analytical recommendation history; forward-fix")
    op.drop_table("purchase_recommendation_decisions")
    op.drop_index("ix_purchase_recommendation_scope_time", table_name="purchase_recommendations")
    op.drop_table("purchase_recommendations")
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("DROP FUNCTION deny_purchase_recommendation_mutation()"))
