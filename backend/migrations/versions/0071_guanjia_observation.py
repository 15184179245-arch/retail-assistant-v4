"""Retain scoped source observations alongside exact Pi tool receipts."""
from alembic import op
import sqlalchemy as sa

revision = '0071_guanjia_observation'
down_revision = '0070_pi_discovery_profile'
branch_labels = depends_on = None


def upgrade():
    op.create_table('guanjia_observation_receipts',
        sa.Column('id',sa.String(36),primary_key=True),
        *[sa.Column(k,sa.String(36),nullable=False) for k in
          ('organization_id','store_id','requester_id','agent_id','run_id','step_id','pi_receipt_id','product_id','snapshot_id')],
        sa.Column('step_kind',sa.String(16),nullable=False),
        sa.Column('product_version',sa.Integer(),nullable=False),
        sa.Column('snapshot_sha256',sa.String(64),nullable=False),
        sa.Column('snapshot_content_sha256',sa.String(64),nullable=False),
        sa.Column('operation',sa.String(24),nullable=False),
        sa.Column('result',sa.JSON(),nullable=False),
        sa.Column('result_sha256',sa.String(64),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('organization_id','store_id','run_id','step_id',name='uq_gj_observation_step'),
        sa.UniqueConstraint('pi_receipt_id',name='uq_gj_observation_native_receipt'),
        sa.ForeignKeyConstraint(['organization_id','store_id','run_id','step_id','step_kind'],
            ['agent_steps.organization_id','agent_steps.store_id','agent_steps.run_id','agent_steps.id','agent_steps.kind'],name='fk_gj_observation_step',ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['pi_receipt_id'],['agent_pi_tool_receipts.id'],name='fk_gj_observation_native',ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['organization_id','store_id','product_id'],
            ['catalog_products.organization_id','catalog_products.store_id','catalog_products.id'],name='fk_gj_observation_product',ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['organization_id','store_id','snapshot_id'],
            ['catalog_snapshots.organization_id','catalog_snapshots.store_id','catalog_snapshots.id'],name='fk_gj_observation_snapshot',ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['organization_id','requester_id'],['subjects.organization_id','subjects.id'],name='fk_gj_observation_requester',ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['organization_id','agent_id'],['subjects.organization_id','subjects.id'],name='fk_gj_observation_agent',ondelete='RESTRICT'),
        sa.CheckConstraint("step_kind = 'tool' AND product_version > 0",name='ck_gj_observation_identity'),
        sa.CheckConstraint("operation IN ('live_prices','live_stock')",name='ck_gj_observation_operation'),
        sa.CheckConstraint('length(result_sha256) = 64 AND length(snapshot_sha256) = 64 AND length(snapshot_content_sha256) = 64',name='ck_gj_observation_digest'))


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError('guanjia_observation_downgrade_requires_data_check')
    if op.get_bind().execute(sa.text('SELECT 1 FROM guanjia_observation_receipts LIMIT 1')).first() is not None:
        raise RuntimeError('guanjia_observations_require_safe_forward_recovery')
    op.drop_table('guanjia_observation_receipts')
