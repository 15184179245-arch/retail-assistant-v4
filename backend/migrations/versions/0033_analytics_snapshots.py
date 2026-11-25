"""ANA-002 immutable partial-inventory snapshots.

Revision ID: 0033_analytics_snapshots
Revises: 0032_receiving_batches
"""
from alembic import op
import sqlalchemy as sa
from backend.app.modules.agents.models import UTCDateTime

revision = "0033_analytics_snapshots"
down_revision = "0032_receiving_batches"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("analytics_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_session_version", sa.Integer(), nullable=False),
        sa.Column("metric_id", sa.String(120), nullable=False),
        sa.Column("metric_version", sa.String(40), nullable=False),
        sa.Column("formula_version", sa.String(80), nullable=False),
        sa.Column("formula_digest", sa.String(64), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("result_digest", sa.String(64), nullable=False),
        sa.Column("coverage_kind", sa.String(20), nullable=False),
        sa.Column("confidence", sa.String(30), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("source_last_recorded_at", UTCDateTime()),
        sa.Column("snapshot_at", UTCDateTime(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id","store_id","id",name="uq_analytics_snapshots_scope"),
        sa.ForeignKeyConstraint(["organization_id","store_id","session_id"],
            ["stocktake_sessions.organization_id","stocktake_sessions.store_id",
             "stocktake_sessions.id"],name="fk_analytics_snapshot_stocktake",ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id","created_by"],
            ["subjects.organization_id","subjects.id"],
            name="fk_analytics_snapshot_creator",ondelete="RESTRICT"),
        sa.CheckConstraint("version > 0 AND source_session_version > 0",
            name="ck_analytics_snapshot_versions"),
        sa.CheckConstraint("length(formula_digest)=64 AND length(source_digest)=64 "
            "AND length(result_digest)=64",name="ck_analytics_snapshot_hashes"),
        sa.CheckConstraint("coverage_kind IN ('partial_area','partial_shelf','unknown')",
            name="ck_analytics_snapshot_partial"),
        sa.CheckConstraint("confidence IN ('partial_reviewed','partial_unreviewed',"
            "'unknown_coverage','insufficient_evidence')",
            name="ck_analytics_snapshot_confidence"),
    )
    op.create_index("ix_analytics_snapshot_scope_time", "analytics_snapshots",
                    ["organization_id","store_id","snapshot_at","id"])
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for action in ("UPDATE","DELETE"):
            op.execute(f"CREATE TRIGGER analytics_snapshot_block_{action.lower()} "
                       f"BEFORE {action} ON analytics_snapshots BEGIN "
                       "SELECT RAISE(ABORT,'analytics snapshot is immutable'); END")
    elif bind.dialect.name == "postgresql":
        op.execute("CREATE FUNCTION analytics_snapshot_immutable() RETURNS trigger AS $$ "
                   "BEGIN RAISE EXCEPTION 'analytics snapshot is immutable'; END; $$ LANGUAGE plpgsql")
        op.execute("CREATE TRIGGER analytics_snapshot_guard BEFORE UPDATE OR DELETE "
                   "ON analytics_snapshots FOR EACH ROW "
                   "EXECUTE FUNCTION analytics_snapshot_immutable()")


def downgrade():
    if op.get_bind().execute(sa.text(
            "SELECT 1 FROM analytics_snapshots LIMIT 1")).first():
        raise RuntimeError("0033 analytics snapshot downgrade refused with data; forward-fix")
    op.drop_index("ix_analytics_snapshot_scope_time", table_name="analytics_snapshots")
    op.drop_table("analytics_snapshots")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION analytics_snapshot_immutable()")
