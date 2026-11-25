"""Add candidate category receipts without changing existing source facts."""
from alembic import op
import sqlalchemy as sa

revision = '0073_guanjia_category_receipts'
down_revision = '0072_pi_source_profile'
branch_labels = depends_on = None


def upgrade():
    op.create_table('guanjia_category_receipts',
        sa.Column('id', sa.String(36), primary_key=True),
        *[sa.Column(key, sa.String(36), nullable=False) for key in
          ('organization_id', 'store_id', 'requester_id', 'agent_id', 'run_id', 'step_id', 'pi_receipt_id')],
        sa.Column('step_kind', sa.String(16), nullable=False),
        sa.Column('operation', sa.String(24), nullable=False),
        *[sa.Column(key, sa.String(64), nullable=False) for key in
          ('input_sha256', 'directory_artifact_sha256', 'directory_content_sha256', 'result_sha256')],
        sa.Column('directory_observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('result', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('organization_id', 'store_id', 'run_id', 'step_id', name='uq_gj_category_step'),
        sa.UniqueConstraint('pi_receipt_id', name='uq_gj_category_native_receipt'),
        sa.ForeignKeyConstraint(['organization_id', 'store_id', 'run_id', 'step_id', 'step_kind'],
            ['agent_steps.organization_id', 'agent_steps.store_id', 'agent_steps.run_id', 'agent_steps.id', 'agent_steps.kind'],
            name='fk_gj_category_step', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['pi_receipt_id'], ['agent_pi_tool_receipts.id'],
            name='fk_gj_category_native', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['organization_id', 'requester_id'], ['subjects.organization_id', 'subjects.id'],
            name='fk_gj_category_requester', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['organization_id', 'agent_id'], ['subjects.organization_id', 'subjects.id'],
            name='fk_gj_category_agent', ondelete='RESTRICT'),
        sa.CheckConstraint("step_kind = 'tool'", name='ck_gj_category_kind'),
        sa.CheckConstraint("operation IN ('category_discovery', 'category_products')", name='ck_gj_category_operation'),
        sa.CheckConstraint('length(input_sha256) = 64 AND length(directory_artifact_sha256) = 64 '
            'AND length(directory_content_sha256) = 64 AND length(result_sha256) = 64', name='ck_gj_category_digest'))


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError('guanjia_category_downgrade_requires_data_check')
    if op.get_bind().execute(sa.text('SELECT 1 FROM guanjia_category_receipts LIMIT 1')).first() is not None:
        raise RuntimeError('guanjia_category_facts_require_safe_forward_recovery')
    op.drop_table('guanjia_category_receipts')
