"""Persistent in-flight instructions only; no legacy backfill or dispatch."""
from alembic import op
import sqlalchemy as sa
revision='0068_assistant_inbox'
down_revision='0067_assistant_turn_execution'
branch_labels=depends_on=None

def upgrade():
    op.create_table('assistant_inbox',
        sa.Column('id', sa.String(36), nullable=False, primary_key=True),
        sa.Column('root_work_id', sa.String(36), nullable=False),
        sa.Column('parent_id', sa.String(36), nullable=False),
        sa.Column('parent_run_id', sa.String(36), nullable=False),
        sa.Column('parent_source_sha256', sa.String(64), nullable=False),
        sa.Column('previous_message_id', sa.String(36), nullable=True),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('organization_id', sa.String(36), nullable=False),
        sa.Column('store_id', sa.String(36), nullable=False),
        sa.Column('owner_id', sa.String(36), nullable=False),
        sa.Column('account_id', sa.String(36), nullable=False),
        sa.Column('origin_session_id', sa.String(36), nullable=False),
        sa.Column('account_version', sa.Integer(), nullable=False),
        sa.Column('work_epoch', sa.Integer(), nullable=False),
        sa.Column('request_id', sa.String(36), nullable=False),
        sa.Column('request_digest', sa.String(64), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('permissions', sa.JSON(), nullable=False),
        sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('binding_sha256', sa.String(64), nullable=False),
        sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint('expires_at > issued_at', name='ck_inbox_expiry'),
        sa.CheckConstraint('length(request_digest)=64 AND length(binding_sha256)=64 AND length(parent_source_sha256)=64', name='ck_inbox_hashes'),
        sa.CheckConstraint('sequence > 0 AND account_version > 0 AND work_epoch > 0', name='ck_inbox_versions'),
        sa.ForeignKeyConstraint(['account_id'], ['employee_accounts.id'], name='fk_inbox_account', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['organization_id', 'owner_id'], ['subjects.organization_id', 'subjects.id'], name='fk_inbox_owner', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['previous_message_id'], ['assistant_inbox.id'], name='fk_inbox_previous', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['root_work_id'], ['assistant_work.task_id'], name='fk_inbox_root', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['origin_session_id'], ['auth_sessions.id'], name='fk_inbox_session', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['organization_id', 'store_id'], ['stores.organization_id', 'stores.id'], name='fk_inbox_store', ondelete="RESTRICT"),
        sa.UniqueConstraint('organization_id', 'store_id', 'owner_id', 'request_id', name='uq_inbox_request'),
        sa.UniqueConstraint('root_work_id', 'sequence', name='uq_inbox_sequence'))

def downgrade():
    if op.get_context().as_sql: raise RuntimeError('assistant_inbox_downgrade_requires_data_check')
    if op.get_bind().execute(sa.text('SELECT 1 FROM assistant_inbox LIMIT 1')).first():
        raise RuntimeError('assistant_inbox_requires_safe_forward_recovery')
    op.drop_table('assistant_inbox')
