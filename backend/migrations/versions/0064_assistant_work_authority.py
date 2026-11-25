"""Separate work revocation epoch and exact-work authority; no legacy backfill."""
from alembic import op
import sqlalchemy as sa

revision = '0064_assistant_work_authority'
down_revision = '0063_agent_message_text'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('scope_security_fences', sa.Column('work_version', sa.Integer(),
        sa.CheckConstraint('work_version > 0', name='ck_scope_security_fence_work_version'),
        nullable=False, server_default='1'))
    op.execute('UPDATE scope_security_fences SET work_version = version')
    op.create_table('assistant_work_authorities',
        sa.Column('work_id', sa.String(36), primary_key=True),
        sa.Column('run_id', sa.String(36), nullable=False),
        sa.Column('organization_id', sa.String(36), nullable=False),
        sa.Column('store_id', sa.String(36), nullable=False),
        sa.Column('owner_id', sa.String(36), nullable=False),
        sa.Column('account_id', sa.String(36), nullable=False),
        sa.Column('origin_session_id', sa.String(36), nullable=False),
        sa.Column('account_version', sa.Integer(), nullable=False),
        sa.Column('work_epoch', sa.Integer(), nullable=False),
        sa.Column('request_id', sa.String(36), nullable=False),
        sa.Column('request_digest', sa.String(64), nullable=False),
        sa.Column('snapshot_sha256', sa.String(64), nullable=False),
        sa.Column('permissions', sa.JSON(), nullable=False),
        sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('binding_sha256', sa.String(64), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['work_id'], ['assistant_work.task_id'], name='fk_work_authority_work', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['account_id'], ['employee_accounts.id'], name='fk_work_authority_account', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['origin_session_id'], ['auth_sessions.id'], name='fk_work_authority_session', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['organization_id', 'store_id'], ['stores.organization_id', 'stores.id'], name='fk_work_authority_store', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['organization_id', 'owner_id'], ['subjects.organization_id', 'subjects.id'], name='fk_work_authority_owner', ondelete='RESTRICT'),
        sa.UniqueConstraint('run_id'),
        sa.CheckConstraint('account_version > 0 AND work_epoch > 0', name='ck_work_authority_versions'),
        sa.CheckConstraint('expires_at > issued_at', name='ck_work_authority_expiry'),
        sa.CheckConstraint('length(request_digest)=64 AND length(snapshot_sha256)=64 AND length(binding_sha256)=64', name='ck_work_authority_hashes'))


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError('work_authority_downgrade_requires_data_check')
    if op.get_bind().execute(sa.text('SELECT 1 FROM assistant_work_authorities LIMIT 1')).first():
        raise RuntimeError('work_authority_requires_safe_forward_recovery')
    op.drop_table('assistant_work_authorities')
    op.drop_column('scope_security_fences', 'work_version')
