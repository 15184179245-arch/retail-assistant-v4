"""Host content provenance metadata; no bodies or historical backfill."""
from alembic import op
import sqlalchemy as sa

revision = '0057_pi_provider_content_digest'
down_revision = '0056_pi_provider_receipts'
branch_labels = None
depends_on = None


def digest_check():
    remaining = 'message_digest'
    for digit in '0123456789abcdef':
        remaining = f"replace({remaining}, '{digit}', '')"
    return f"message_digest IS NULL OR (length(message_digest) = 64 AND {remaining} = '')"


def upgrade():
    with op.batch_alter_table('agent_pi_provider_receipts') as batch:
        batch.add_column(sa.Column('message_digest', sa.String(64), nullable=True))
        batch.add_column(sa.Column('usage_presence', sa.JSON(none_as_null=True), nullable=True))
        batch.create_check_constraint('ck_pi_provider_message_digest', digest_check())


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError('pi_provider_downgrade_requires_verified_empty_table')
    if op.get_bind().execute(sa.text('SELECT 1 FROM agent_pi_provider_receipts LIMIT 1')).first() is not None:
        raise RuntimeError('pi_provider_facts_require_safe_forward_recovery')
    with op.batch_alter_table('agent_pi_provider_receipts') as batch:
        batch.drop_constraint('ck_pi_provider_message_digest', type_='check')
        batch.drop_column('usage_presence')
        batch.drop_column('message_digest')
