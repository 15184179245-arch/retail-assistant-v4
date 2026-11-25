"""DSH orchestration metadata for existing Tasks, with loss-refusing rollback."""
from alembic import op
import sqlalchemy as sa

revision = '0061_assistant_work_intake'
down_revision = '0060_pi_memory_provider_profile'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('assistant_work',
        sa.Column('task_id', sa.String(36), primary_key=True),
        sa.Column('organization_id', sa.String(36), nullable=False),
        sa.Column('store_id', sa.String(36), nullable=False),
        sa.Column('owner_id', sa.String(36), nullable=False),
        sa.Column('origin_account_id', sa.String(36), nullable=False),
        sa.Column('request_id', sa.String(36), nullable=False),
        sa.Column('request_digest', sa.String(64), nullable=False),
        sa.Column('core_commit', sa.String(40), nullable=False),
        sa.Column('ticket_id', sa.String(32), nullable=False),
        sa.Column('assignment_id', sa.String(32), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False),
        sa.Column('snapshot_sha256', sa.String(64), nullable=False),
        sa.Column('outbox_event_id', sa.String(36), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['organization_id','store_id','task_id'],
            ['tasks.organization_id','tasks.store_id','tasks.id'], name='fk_assistant_work_task', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['organization_id','owner_id'], ['subjects.organization_id','subjects.id'],
            name='fk_assistant_work_owner', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['origin_account_id'], ['employee_accounts.id'], name='fk_assistant_work_account', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['outbox_event_id'], ['outbox_events.id'], name='fk_assistant_work_outbox', ondelete='RESTRICT'),
        sa.UniqueConstraint('organization_id','store_id','owner_id','request_id',name='uq_assistant_work_request'),
        sa.UniqueConstraint('outbox_event_id',name='uq_assistant_work_outbox'),
        sa.CheckConstraint('version > 0',name='ck_assistant_work_version'),
        sa.CheckConstraint('length(request_digest)=64 AND length(snapshot_sha256)=64 AND length(core_commit)=40 '
            'AND length(ticket_id)=32 AND length(assignment_id)=32',name='ck_assistant_work_bindings'))


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError('assistant_work_downgrade_requires_data_check')
    if op.get_bind().execute(sa.text('SELECT 1 FROM assistant_work LIMIT 1')).first() is not None:
        raise RuntimeError('assistant_work_requires_safe_forward_recovery')
    op.drop_table('assistant_work')
