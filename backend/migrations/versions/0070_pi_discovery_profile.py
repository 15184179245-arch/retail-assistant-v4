"""Admit explicit discovery receipts without altering existing provider facts."""
from alembic import op
import sqlalchemy as sa

revision = '0070_pi_discovery_profile'
down_revision = '0069_assistant_inbox_execution'
branch_labels = depends_on = None
_PREFIX = "model_kind = 'model' AND epoch > 0 AND model = 'deepseek-v4.1-flash' AND "
_OLD = "wire_protocol IN ('v4.pi-readonly/2', 'v4.pi-memory/2')"
_NEW = "wire_protocol IN ('v4.pi-readonly/2', 'v4.pi-memory/2', 'v4.pi-discovery/2')"


def upgrade():
    with op.batch_alter_table('agent_pi_provider_receipts') as batch:
        batch.drop_constraint('ck_pi_provider_identity', type_='check')
        batch.create_check_constraint('ck_pi_provider_identity', _PREFIX + _NEW)


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError('pi_discovery_downgrade_requires_data_check')
    if op.get_bind().execute(sa.text("SELECT 1 FROM agent_pi_provider_receipts WHERE wire_protocol = 'v4.pi-discovery/2' LIMIT 1")).first() is not None:
        raise RuntimeError('pi_discovery_facts_require_safe_forward_recovery')
    with op.batch_alter_table('agent_pi_provider_receipts') as batch:
        batch.drop_constraint('ck_pi_provider_identity', type_='check')
        batch.create_check_constraint('ck_pi_provider_identity', _PREFIX + _OLD)
