"""Admit the AGENT-03 workbench provider facts; preserve every existing fact.

The unified runtime's workbench contract pins wire protocol 'v4.pi-workbench/2'
(pi_runtime_contract.py). A workbench Run's first model_begin inserts an
agent_pi_provider_receipts row carrying that wire value; without widening the
identity CHECK the insert fails closed as an integrity error and the Run dies
with zero model calls. This migration only widens the allowed IN list: every
previously permitted row remains permitted, and the downgrade refuses to run
while any workbench fact exists (safe-forward recovery, mirroring 0075).
"""
from alembic import op
import sqlalchemy as sa

revision='0083_pi_workbench_provider_profile'
down_revision='0082_assistant_workspace_binding'
branch_labels=depends_on=None
_PREFIX="model_kind = 'model' AND epoch > 0 AND model = 'deepseek-v4.1-flash' AND "
_OLD="wire_protocol IN ('v4.pi-readonly/2', 'v4.pi-memory/2', 'v4.pi-discovery/2', 'v4.pi-source/2', 'v4.pi-readonly/3')"
_NEW="wire_protocol IN ('v4.pi-readonly/2', 'v4.pi-memory/2', 'v4.pi-discovery/2', 'v4.pi-source/2', 'v4.pi-readonly/3', 'v4.pi-workbench/2')"


def upgrade():
    with op.batch_alter_table('agent_pi_provider_receipts') as batch:
        batch.drop_constraint('ck_pi_provider_identity',type_='check')
        batch.create_check_constraint('ck_pi_provider_identity',_PREFIX+_NEW)


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError('pi_workbench_downgrade_requires_data_check')
    if op.get_bind().execute(sa.text("SELECT 1 FROM agent_pi_provider_receipts WHERE wire_protocol='v4.pi-workbench/2' LIMIT 1")).first() is not None:
        raise RuntimeError('pi_workbench_facts_require_safe_forward_recovery')
    with op.batch_alter_table('agent_pi_provider_receipts') as batch:
        batch.drop_constraint('ck_pi_provider_identity',type_='check')
        batch.create_check_constraint('ck_pi_provider_identity',_PREFIX+_OLD)
