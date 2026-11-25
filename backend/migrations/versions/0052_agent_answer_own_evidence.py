"""Answer receipts: store the run's own captured sources separately.

The receipt keeps the full fail-closed evidence chain for availability
checking, and additionally records the source references captured from this
run's own tool calls, so readers can render what this answer actually saw
instead of the whole conversation evidence pool.
"""
from alembic import op
import sqlalchemy as sa

revision='0052_agent_answer_own_evidence'
down_revision='0051_postgres_evidence_guards'
branch_labels=None
depends_on=None


def upgrade():
    op.add_column('agent_answers', sa.Column('own_evidence', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('agent_answers', 'own_evidence')
