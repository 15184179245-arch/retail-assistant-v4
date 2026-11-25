"""Explicit Pi memory wire, preserving provider receipt facts and old profile."""
from alembic import op
import sqlalchemy as sa

revision = '0060_pi_memory_provider_profile'
down_revision = '0059_memory_durable_retention'
branch_labels = None
depends_on = None
_PREFIX = "model_kind = 'model' AND epoch > 0 AND model = 'deepseek-v4.1-flash' AND "


def upgrade():
    with op.batch_alter_table('agent_pi_provider_receipts') as batch:
        batch.drop_constraint('ck_pi_provider_identity', type_='check')
        batch.create_check_constraint('ck_pi_provider_identity',
            _PREFIX + "wire_protocol IN ('v4.pi-readonly/2', 'v4.pi-memory/2')")


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError('pi_memory_downgrade_requires_data_check')
    if op.get_bind().execute(sa.text("SELECT 1 FROM agent_pi_provider_receipts WHERE wire_protocol = 'v4.pi-memory/2' LIMIT 1")).first() is not None:
        raise RuntimeError('pi_memory_facts_require_safe_forward_recovery')
    with op.batch_alter_table('agent_pi_provider_receipts') as batch:
        batch.drop_constraint('ck_pi_provider_identity', type_='check')
        batch.create_check_constraint('ck_pi_provider_identity', _PREFIX + "wire_protocol = 'v4.pi-readonly/2'")
