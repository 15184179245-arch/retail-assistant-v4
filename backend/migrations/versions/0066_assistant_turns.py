"""Immutable followup intake on the same Task; no execution authority backfill."""
from alembic import op
import sqlalchemy as sa
revision='0066_assistant_turns'
down_revision='0065_assistant_progress'
branch_labels=depends_on=None

def upgrade():
    op.create_table('assistant_turns',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('root_work_id',sa.String(36),nullable=False),
        sa.Column('previous_turn_id',sa.String(36),nullable=False),
        sa.Column('sequence',sa.Integer(),nullable=False),
        sa.Column('organization_id',sa.String(36),nullable=False),
        sa.Column('store_id',sa.String(36),nullable=False),
        sa.Column('owner_id',sa.String(36),nullable=False),
        sa.Column('origin_account_id',sa.String(36),nullable=False),
        sa.Column('request_id',sa.String(36),nullable=False),
        sa.Column('request_digest',sa.String(64),nullable=False),
        sa.Column('text',sa.Text(),nullable=False),
        sa.Column('run_id',sa.String(36),nullable=False),
        sa.Column('ticket_id',sa.String(32),nullable=False),
        sa.Column('assignment_id',sa.String(32),nullable=False),
        sa.Column('parent_fact',sa.JSON(),nullable=False),
        sa.Column('parent_fact_sha256',sa.String(64),nullable=False),
        sa.Column('history',sa.JSON(),nullable=False),
        sa.Column('history_sha256',sa.String(64),nullable=False),
        sa.Column('snapshot',sa.JSON(),nullable=False),
        sa.Column('snapshot_sha256',sa.String(64),nullable=False),
        sa.Column('manifest_sha256',sa.String(64),nullable=False),
        sa.Column('state',sa.String(16),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.ForeignKeyConstraint(['root_work_id'],['assistant_work.task_id'],name='fk_assistant_turn_root',ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['organization_id','store_id','root_work_id'],['tasks.organization_id','tasks.store_id','tasks.id'],name='fk_assistant_turn_task_scope',ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['organization_id','owner_id'],['subjects.organization_id','subjects.id'],name='fk_assistant_turn_owner',ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['origin_account_id'],['employee_accounts.id'],name='fk_assistant_turn_account',ondelete='RESTRICT'),
        sa.UniqueConstraint('root_work_id','sequence',name='uq_assistant_turn_sequence'),
        sa.UniqueConstraint('organization_id','store_id','owner_id','request_id',name='uq_assistant_turn_request'),
        sa.UniqueConstraint('run_id',name='uq_assistant_turn_run'),
        sa.CheckConstraint('sequence > 1',name='ck_assistant_turn_sequence'),
        sa.CheckConstraint("state = 'received'",name='ck_assistant_turn_state'),
        sa.CheckConstraint('length(request_digest)=64 AND length(parent_fact_sha256)=64 AND length(history_sha256)=64 AND length(snapshot_sha256)=64 AND length(manifest_sha256)=64',name='ck_assistant_turn_hashes'),
        sa.CheckConstraint('length(ticket_id)=32 AND length(assignment_id)=32',name='ck_assistant_turn_core_ids'))

def downgrade():
    if op.get_context().as_sql:raise RuntimeError('assistant_turns_downgrade_requires_data_check')
    if op.get_bind().execute(sa.text('SELECT 1 FROM assistant_turns LIMIT 1')).first():
        raise RuntimeError('assistant_turns_require_safe_forward_recovery')
    op.drop_table('assistant_turns')
