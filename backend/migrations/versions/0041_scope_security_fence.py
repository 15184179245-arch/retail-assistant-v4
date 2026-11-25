"""AGT-026 B1 durable scope security fence. Revision 0041_scope_security_fence after 0040.

Creates the per organization/store authorization epoch row, seeds epoch 1 for
every existing store, and reserves the execution-side authorization fields that
the later worker enforcement will compare against. Legacy execution rows keep a
NULL epoch: the migration never backfills one.
"""
from alembic import op
import sqlalchemy as sa

revision = "0041_scope_security_fence"
down_revision = "0040_fin_workpapers"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("scope_security_fences",
        sa.Column("organization_id", sa.String(36), primary_key=True),
        sa.Column("store_id", sa.String(36), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
            ["stores.organization_id", "stores.id"],
            name="fk_scope_security_fences_store_scope", ondelete="RESTRICT"),
        sa.CheckConstraint("version > 0", name="ck_scope_security_fence_version"))
    # Existing stores start at epoch 1. Every later revocation advances it.
    op.execute(sa.text(
        "INSERT INTO scope_security_fences (organization_id, store_id, version, updated_at) "
        "SELECT organization_id, id, 1, CURRENT_TIMESTAMP FROM stores"))
    with op.batch_alter_table("agent_run_executions") as batch:
        batch.add_column(sa.Column("authorization_epoch", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("approval_grant", sa.JSON(), nullable=True))
        batch.create_check_constraint("ck_agent_execution_authorization_epoch",
            "authorization_epoch IS NULL OR authorization_epoch > 0")


def downgrade():
    # Contract: keep the table, columns and data whenever either side holds any
    # row. Any execution row blocks the rollback, including a legacy row whose
    # reserved fields are still NULL: dropping the columns would erase the place
    # the later worker segment must record its epoch. Only a completely empty new
    # structure may roll back; a data-bearing database is forward-fixed.
    bind = op.get_bind()
    if bind.execute(sa.text(
            "SELECT 1 FROM scope_security_fences LIMIT 1")).first():
        raise RuntimeError(
            "0041 downgrade refused while a scope security fence row exists; forward-fix")
    if bind.execute(sa.text(
            "SELECT 1 FROM agent_run_executions LIMIT 1")).first():
        raise RuntimeError(
            "0041 downgrade refused while execution data exists; forward-fix")
    with op.batch_alter_table("agent_run_executions") as batch:
        # SQLite rebuilds the table for batch operations and keeps the CHECK in
        # the DDL, so it must be dropped explicitly on every dialect.
        batch.drop_constraint("ck_agent_execution_authorization_epoch", type_="check")
        batch.drop_column("approval_grant")
        batch.drop_column("authorization_epoch")
    op.drop_table("scope_security_fences")
