"""Durable Pi launch intents; never invent authority for older work."""
from alembic import op
import sqlalchemy as sa

revision = '0062_assistant_pi_dispatch'
down_revision = '0061_assistant_work_intake'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('assistant_dispatch',
        sa.Column('work_id', sa.String(36), primary_key=True),
        sa.Column('run_id', sa.String(36), nullable=False),
        sa.Column('session_id', sa.String(36), nullable=False),
        sa.Column('account_version', sa.Integer(), nullable=False),
        sa.Column('authorization_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('runtime_record', sa.JSON(), nullable=True),
        sa.Column('machine_id', sa.String(36), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reconcile_after', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('code', sa.String(80), nullable=True),
        sa.ForeignKeyConstraint(['work_id'], ['assistant_work.task_id'], name='fk_assistant_dispatch_work', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['session_id'], ['auth_sessions.id'], name='fk_assistant_dispatch_session', ondelete='RESTRICT'),
        sa.UniqueConstraint('run_id'),
        sa.CheckConstraint('account_version > 0', name='ck_assistant_dispatch_account_version'),
        sa.CheckConstraint("state IN ('pending','launching','settled','denied','unknown')", name='ck_assistant_dispatch_state'))


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError('assistant_dispatch_downgrade_requires_data_check')
    if op.get_bind().execute(sa.text('SELECT 1 FROM assistant_dispatch LIMIT 1')).first() is not None:
        raise RuntimeError('assistant_dispatch_requires_safe_forward_recovery')
    op.drop_table('assistant_dispatch')
