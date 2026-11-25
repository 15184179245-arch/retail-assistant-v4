"""Separate POS delivery reference from receipt external_ref (SALE-004).

Revision ID: 0018_sales_handoff_delivery_ref
Revises: 0017_sales_draft

Existing handoffs cannot recover a trustworthy delivery reference: earlier code
could overwrite external_ref with a receipt reference. The column is therefore
added as NULL for legacy rows; receipt polling refuses to run and delivery
refuses to auto-resend until an operator reconciles the legacy handoff.
"""
from alembic import op
import sqlalchemy as sa

revision = "0018_sales_handoff_delivery_ref"
down_revision = "0017_sales_draft"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sales_handoffs", sa.Column("delivery_ref", sa.String(200), nullable=True))


def downgrade() -> None:
    # 0017 refuses to drop its tables while sales rows exist; dropping the
    # column first would therefore leak a partial downgrade and destroy the
    # only persisted delivery reference. Refuse before any destructive step.
    connection = op.get_bind()
    count = connection.execute(sa.text("SELECT COUNT(*) FROM sales_handoffs")).scalar()
    if count:
        raise RuntimeError("sales handoff delivery_ref downgrade refused")
    with op.batch_alter_table("sales_handoffs") as batch:
        batch.drop_column("delivery_ref")
