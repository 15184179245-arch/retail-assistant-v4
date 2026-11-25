"""Forward-only knowledge error storage; no Host/Node capability grant.

Old 0076 is immutable. Live SQLite accepts only the exact historical or exact
author-expanded shape, within an owned FK-ON physical transaction.
"""
from alembic import op
from backend.migrations.pi_result_forward import run_forward_step

revision = "0079_pi_knowledge_tool_error"
down_revision = "0078_merge_workspace_pi"
branch_labels = depends_on = None


def upgrade():
    if op.get_context().as_sql:
        raise RuntimeError("pi_knowledge_forward_requires_live_shape_check")
    run_forward_step(op.get_bind(), op.get_context())


def downgrade():
    raise RuntimeError("pi_knowledge_forward_only_restore_verified_backup_or_forward_repair")
