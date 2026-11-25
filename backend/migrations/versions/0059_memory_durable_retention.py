"""Explicit until-withdrawn personal memory; preserve every legacy deadline."""
from alembic import op
import sqlalchemy as sa

revision = '0059_memory_durable_retention'
down_revision = '0058_pi_go_usage_basis'
branch_labels = None
depends_on = None

CHECK = ('(retain_until_withdrawn AND persistent AND expires_at IS NULL) OR '
         '(NOT retain_until_withdrawn AND expires_at IS NOT NULL)')


def upgrade():
    with op.batch_alter_table('personal_memories') as batch:
        batch.add_column(sa.Column('retain_until_withdrawn', sa.Boolean(),
                                   nullable=False, server_default=sa.false()))
        batch.alter_column('expires_at', existing_type=sa.DateTime(timezone=True), nullable=True)
        batch.create_check_constraint('ck_personal_memories_retention', CHECK)


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError('memory_retention_downgrade_requires_data_check')
    bind = op.get_bind()
    memories = sa.table('personal_memories', sa.column('retain_until_withdrawn', sa.Boolean()))
    commands = sa.table('memory_commands', sa.column('response', sa.JSON()))
    if (bind.execute(sa.select(1).select_from(memories).where(
            memories.c.retain_until_withdrawn.is_(True)).limit(1)).first() is not None
            or bind.execute(sa.select(1).select_from(commands).where(
                commands.c.response['retain_until_withdrawn'].as_boolean().is_(True)).limit(1)).first() is not None):
        raise RuntimeError('memory_retention_facts_require_safe_forward_recovery')
    with op.batch_alter_table('personal_memories') as batch:
        batch.drop_constraint('ck_personal_memories_retention', type_='check')
        batch.alter_column('expires_at', existing_type=sa.DateTime(timezone=True), nullable=False)
        batch.drop_column('retain_until_withdrawn')
