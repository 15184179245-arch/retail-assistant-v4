"""Admit readonly /3 provider facts; preserve every existing constraint/fact."""
from alembic import op
import sqlalchemy as sa

revision='0075_pi_readonly_stream_profile'
down_revision='0074_skill_resource_manifests'
branch_labels=depends_on=None
_PREFIX="model_kind = 'model' AND epoch > 0 AND model = 'deepseek-v4.1-flash' AND "
_OLD="wire_protocol IN ('v4.pi-readonly/2', 'v4.pi-memory/2', 'v4.pi-discovery/2', 'v4.pi-source/2')"
_NEW="wire_protocol IN ('v4.pi-readonly/2', 'v4.pi-memory/2', 'v4.pi-discovery/2', 'v4.pi-source/2', 'v4.pi-readonly/3')"


def upgrade():
    with op.batch_alter_table('agent_pi_provider_receipts') as batch:
        batch.drop_constraint('ck_pi_provider_identity',type_='check')
        batch.create_check_constraint('ck_pi_provider_identity',_PREFIX+_NEW)


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError('pi_stream_downgrade_requires_data_check')
    if op.get_bind().execute(sa.text("SELECT 1 FROM agent_pi_provider_receipts WHERE wire_protocol='v4.pi-readonly/3' LIMIT 1")).first() is not None:
        raise RuntimeError('pi_stream_facts_require_safe_forward_recovery')
    with op.batch_alter_table('agent_pi_provider_receipts') as batch:
        batch.drop_constraint('ck_pi_provider_identity',type_='check')
        batch.create_check_constraint('ck_pi_provider_identity',_PREFIX+_OLD)
