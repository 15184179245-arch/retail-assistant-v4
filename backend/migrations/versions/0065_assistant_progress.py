"""Separate immutable DSH execution projection; no legacy data backfill."""
from alembic import op
import sqlalchemy as sa
revision = '0065_assistant_progress'
down_revision = '0064_assistant_work_authority'
branch_labels = depends_on = None

def upgrade():
    op.create_table('assistant_progress',
        sa.Column('work_id',sa.String(36),primary_key=True),
        sa.Column('run_id',sa.String(36),nullable=False),
        sa.Column('state',sa.String(24),nullable=False),
        sa.Column('fact',sa.JSON(),nullable=False),
        sa.Column('fact_sha256',sa.String(64),nullable=False),
        sa.Column('projection',sa.JSON(),nullable=False),
        sa.Column('projection_sha256',sa.String(64),nullable=False),
        sa.Column('manifest_sha256',sa.String(64),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.ForeignKeyConstraint(['work_id'],['assistant_work.task_id'],name='fk_assistant_progress_work',ondelete='RESTRICT'),
        sa.UniqueConstraint('run_id',name='uq_assistant_progress_run'),
        sa.CheckConstraint("state IN ('completed','failed','cancelled','blocked','needs_attention')",name='ck_assistant_progress_state'),
        sa.CheckConstraint('length(fact_sha256)=64 AND length(projection_sha256)=64 AND length(manifest_sha256)=64',name='ck_assistant_progress_hashes'))

def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError('assistant_progress_downgrade_requires_data_check')
    if op.get_bind().execute(sa.text('SELECT 1 FROM assistant_progress LIMIT 1')).first():
        raise RuntimeError('assistant_progress_requires_safe_forward_recovery')
    op.drop_table('assistant_progress')
