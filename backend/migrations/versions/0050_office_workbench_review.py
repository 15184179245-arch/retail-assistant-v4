"""Explicit office review workbench DDL (OFC-009).

Revision ID: 0050_office_workbench_review
Revises: 0049_erp_settlement_dispatch
Create Date: 2026-10-30
"""
from alembic import op
import sqlalchemy as sa

revision = '0050_office_workbench_review'
down_revision = '0049_erp_settlement_dispatch'
branch_labels = None
depends_on = None

TABLES = ('office_review_items', 'office_review_decisions')


def upgrade():
    op.create_table(
        'office_review_items',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('organization_id', sa.String(36), nullable=False),
        sa.Column('store_id', sa.String(36), nullable=False),
        sa.Column('target_kind', sa.String(40), nullable=False),
        sa.Column('target_id', sa.String(36), nullable=False),
        sa.Column('target_version', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('summary', sa.String(1000), nullable=False),
        sa.Column('risk_level', sa.String(20), nullable=False),
        sa.Column('diff_payload', sa.JSON(), nullable=False),
        sa.Column('state', sa.String(20), nullable=False),
        sa.Column('claimed_by', sa.String(36), nullable=True),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('lock_token', sa.String(64), nullable=True),
        sa.Column('lock_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reviewed_by', sa.String(36), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('review_decision', sa.String(20), nullable=True),
        sa.Column('review_notes', sa.String(1000), nullable=True),
        sa.Column('modifications', sa.JSON(), nullable=True),
        sa.Column('receipt_digest', sa.String(64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('organization_id', 'store_id', 'id', name='uq_ori_scope'),
        sa.UniqueConstraint('organization_id', 'store_id', 'target_kind', 'target_id', name='uq_ori_target'),
        sa.ForeignKeyConstraint(
            ['organization_id', 'store_id'],
            ['stores.organization_id', 'stores.id'],
            name='fk_ori_store',
            ondelete='RESTRICT',
        ),
        sa.CheckConstraint("risk_level IN ('low', 'medium', 'high', 'critical')", name='ck_ori_risk'),
        sa.CheckConstraint(
            "state IN ('pending', 'claimed', 'approved', 'rejected', 'modified', 'escalated')",
            name='ck_ori_state',
        ),
        sa.CheckConstraint('target_version >= 1', name='ck_ori_target_ver'),
    )
    op.create_index(
        'ix_ori_state_risk',
        'office_review_items',
        ['organization_id', 'store_id', 'state', 'risk_level'],
    )

    op.create_table(
        'office_review_decisions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('organization_id', sa.String(36), nullable=False),
        sa.Column('store_id', sa.String(36), nullable=False),
        sa.Column('item_id', sa.String(36), nullable=False),
        sa.Column('target_kind', sa.String(40), nullable=False),
        sa.Column('target_id', sa.String(36), nullable=False),
        sa.Column('target_version', sa.Integer(), nullable=False),
        sa.Column('decision', sa.String(20), nullable=False),
        sa.Column('modifications', sa.JSON(), nullable=True),
        sa.Column('actor_id', sa.String(36), nullable=False),
        sa.Column('reason', sa.String(1000), nullable=False),
        sa.Column('receipt_digest', sa.String(64), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('organization_id', 'store_id', 'id', name='uq_ord_scope'),
        sa.ForeignKeyConstraint(
            ['organization_id', 'store_id'],
            ['stores.organization_id', 'stores.id'],
            name='fk_ord_store',
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['organization_id', 'store_id', 'item_id'],
            ['office_review_items.organization_id', 'office_review_items.store_id', 'office_review_items.id'],
            name='fk_ord_item',
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['organization_id', 'actor_id'],
            ['subjects.organization_id', 'subjects.id'],
            name='fk_ord_actor',
            ondelete='RESTRICT',
        ),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected', 'modified', 'escalated')",
            name='ck_ord_decision',
        ),
        sa.CheckConstraint('length(receipt_digest)=64', name='ck_ord_digest'),
    )
    op.create_index(
        'ix_ord_item_decided',
        'office_review_decisions',
        ['organization_id', 'store_id', 'item_id', 'decided_at'],
    )

    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        bind.exec_driver_sql(
            "CREATE TRIGGER trg_office_review_decisions_no_update BEFORE UPDATE ON office_review_decisions "
            "BEGIN SELECT RAISE(ABORT, 'office review decision immutable'); END;"
        )
        bind.exec_driver_sql(
            "CREATE TRIGGER trg_office_review_decisions_no_delete BEFORE DELETE ON office_review_decisions "
            "BEGIN SELECT RAISE(ABORT, 'office review decision immutable'); END;"
        )


def downgrade():
    bind = op.get_bind()
    for table in reversed(TABLES):
        count = bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
        if count > 0:
            raise RuntimeError(f"0050 downgrade refused: {table} contains {count} row(s); preserve audit evidence")

    if bind.dialect.name == 'sqlite':
        bind.exec_driver_sql("DROP TRIGGER IF EXISTS trg_office_review_decisions_no_update;")
        bind.exec_driver_sql("DROP TRIGGER IF EXISTS trg_office_review_decisions_no_delete;")

    for table in reversed(TABLES):
        op.drop_table(table)
