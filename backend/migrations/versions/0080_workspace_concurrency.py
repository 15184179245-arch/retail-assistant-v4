"""Unique Workspace create receipts; preserve every historical artifact.

Existing duplicate identities require explicit reconciliation, never deletion.
This formal 0080 is NOT the separate APFS candidate's Session 0080.
"""
from alembic import op
import sqlalchemy as sa

revision = "0080_workspace_concurrency"
down_revision = "0079_pi_knowledge_tool_error"
branch_labels = depends_on = None
INDEX = "uq_workspace_create_actor_key"
COLUMNS = ["organization_id", "store_id", "actor_subject_id", "idempotency_key"]
PREDICATE = "op_type = 'create' AND idempotency_key IS NOT NULL AND idempotency_key <> ''"


def upgrade():
    if op.get_context().as_sql:
        raise RuntimeError("workspace_create_requires_duplicate_preflight")
    bind = op.get_bind()
    if bind.dialect.name not in ("sqlite", "postgresql"):
        raise RuntimeError("workspace_create_unsupported_dialect")
    if bind.dialect.name == "sqlite":
        root = bind.get_transaction()
        if root is None or not root.is_active or bind.in_nested_transaction():
            raise RuntimeError("workspace_create_migration_root_required")
        raw = bind.connection.driver_connection
        if not raw.in_transaction:
            bind.exec_driver_sql("BEGIN IMMEDIATE")
    duplicates = bind.execute(sa.text(
        "SELECT 1 FROM workspace_artifact_ops WHERE " + PREDICATE +
        " GROUP BY " + ",".join(COLUMNS) + " HAVING COUNT(*) > 1 LIMIT 1"
    )).first()
    if duplicates:
        raise RuntimeError("workspace_create_duplicate_history_requires_reconciliation")
    op.create_index(INDEX, "workspace_artifact_ops", COLUMNS, unique=True,
                    sqlite_where=sa.text(PREDICATE), postgresql_where=sa.text(PREDICATE))


def downgrade():
    raise RuntimeError("workspace_concurrency_forward_only_preserve_create_identities")
