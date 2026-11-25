"""Store complete messages; never truncate on downgrade."""
from alembic import op, context
import sqlalchemy as sa

revision = '0063_agent_message_text'
down_revision = '0062_assistant_pi_dispatch'
branch_labels = None
depends_on = None

def upgrade():
    if op.get_bind().dialect.name == 'postgresql':
        op.alter_column('agent_messages', 'content', existing_type=sa.String(4000), type_=sa.Text(), existing_nullable=False)
    # SQLite VARCHAR and TEXT share TEXT affinity and enforce no length quota.

def downgrade():
    if context.is_offline_mode():
        raise RuntimeError('message_downgrade_requires_data_check')
    bind = op.get_bind()
    if bind.execute(sa.text('SELECT 1 FROM agent_messages WHERE length(content) > 4000 LIMIT 1')).first():
        raise RuntimeError('long_messages_prevent_downgrade')
    if bind.dialect.name == 'postgresql':
        op.alter_column('agent_messages', 'content', existing_type=sa.Text(), type_=sa.String(4000), existing_nullable=False)
