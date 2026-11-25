"""Exact-turn dispatch and authority. No backfill or renewal of old turns."""
from alembic import op
import sqlalchemy as sa
revision='0067_assistant_turn_execution'
down_revision='0066_assistant_turns'
branch_labels=depends_on=None

def upgrade():
    op.create_table('assistant_turn_dispatch',
        sa.Column('work_id',sa.String(36),nullable=False,primary_key=True),
        sa.Column('run_id',sa.String(36),nullable=False),
        sa.Column('session_id',sa.String(36),nullable=False),
        sa.Column('account_version',sa.Integer(),nullable=False),
        sa.Column('authorization_expires_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('state',sa.String(16),nullable=False),
        sa.Column('runtime_record',sa.JSON(),nullable=True),
        sa.Column('machine_id',sa.String(36),nullable=True),
        sa.Column('started_at',sa.DateTime(timezone=True),nullable=True),
        sa.Column('reconcile_after',sa.DateTime(timezone=True),nullable=True),
        sa.Column('finished_at',sa.DateTime(timezone=True),nullable=True),
        sa.Column('code',sa.String(80),nullable=True),
        sa.Column('outbox_event_id',sa.String(36),nullable=False),
        sa.Column('result_record',sa.JSON(),nullable=True),
        sa.Column('result_sha256',sa.String(64),nullable=True),
        sa.UniqueConstraint('run_id',name=None),
        sa.UniqueConstraint('outbox_event_id',name=None),
        sa.CheckConstraint('account_version > 0',name='ck_assistant_turn_dispatch_account_version'),
        sa.CheckConstraint("state IN ('pending','launching','settled','denied','unknown')",name='ck_assistant_turn_dispatch_state'),
        sa.ForeignKeyConstraint(['session_id'],['auth_sessions.id'],name='fk_assistant_turn_dispatch_session',ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['work_id'],['assistant_turns.id'],name='fk_assistant_turn_dispatch_work',ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['outbox_event_id'],['outbox_events.id'],name='fk_turn_dispatch_outbox',ondelete='RESTRICT'))
    op.create_table('assistant_turn_authorities',
        sa.Column('work_id',sa.String(36),nullable=False,primary_key=True),
        sa.Column('run_id',sa.String(36),nullable=False),
        sa.Column('organization_id',sa.String(36),nullable=False),
        sa.Column('store_id',sa.String(36),nullable=False),
        sa.Column('owner_id',sa.String(36),nullable=False),
        sa.Column('account_id',sa.String(36),nullable=False),
        sa.Column('origin_session_id',sa.String(36),nullable=False),
        sa.Column('account_version',sa.Integer(),nullable=False),
        sa.Column('work_epoch',sa.Integer(),nullable=False),
        sa.Column('request_id',sa.String(36),nullable=False),
        sa.Column('request_digest',sa.String(64),nullable=False),
        sa.Column('snapshot_sha256',sa.String(64),nullable=False),
        sa.Column('permissions',sa.JSON(),nullable=False),
        sa.Column('issued_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('expires_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('binding_sha256',sa.String(64),nullable=False),
        sa.Column('revoked_at',sa.DateTime(timezone=True),nullable=True),
        sa.UniqueConstraint('run_id',name=None),
        sa.CheckConstraint('expires_at > issued_at',name='ck_turn_authority_expiry'),
        sa.CheckConstraint('length(request_digest)=64 AND length(snapshot_sha256)=64 AND length(binding_sha256)=64',name='ck_turn_authority_hashes'),
        sa.CheckConstraint('account_version > 0 AND work_epoch > 0',name='ck_turn_authority_versions'),
        sa.ForeignKeyConstraint(['account_id'],['employee_accounts.id'],name='fk_turn_authority_account',ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['organization_id', 'owner_id'],['subjects.organization_id', 'subjects.id'],name='fk_turn_authority_owner',ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['origin_session_id'],['auth_sessions.id'],name='fk_turn_authority_session',ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['organization_id', 'store_id'],['stores.organization_id', 'stores.id'],name='fk_turn_authority_store',ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['work_id'],['assistant_turns.id'],name='fk_turn_authority_work',ondelete='RESTRICT'))

def downgrade():
    if op.get_context().as_sql:raise RuntimeError('turn_execution_downgrade_requires_data_check')
    for name in ('assistant_turn_authorities','assistant_turn_dispatch'):
        if op.get_bind().execute(sa.text('SELECT 1 FROM '+name+' LIMIT 1')).first():
            raise RuntimeError('turn_execution_requires_safe_forward_recovery')
    op.drop_table('assistant_turn_authorities')
    op.drop_table('assistant_turn_dispatch')
