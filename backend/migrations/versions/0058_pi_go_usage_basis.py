"""Private explicit Go usage evidence; additive, no historical backfill."""
from alembic import op
import sqlalchemy as sa

revision = '0058_pi_go_usage_basis'
down_revision = '0057_pi_provider_content_digest'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('agent_pi_provider_receipts') as batch:
        batch.add_column(sa.Column('usage_basis', sa.JSON(none_as_null=True), nullable=True))


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError('pi_provider_downgrade_requires_verified_empty_table')
    if op.get_bind().execute(sa.text('SELECT 1 FROM agent_pi_provider_receipts LIMIT 1')).first() is not None:
        raise RuntimeError('pi_provider_facts_require_safe_forward_recovery')
    with op.batch_alter_table('agent_pi_provider_receipts') as batch:
        batch.drop_column('usage_basis')
