"""Add reviewer and review decision tracking without rebuilding task tables.

Revision ID: 0007_task_policy
Revises: 0006_agent_persistence
"""

from alembic import op
import sqlalchemy as sa

revision = "0007_task_policy"
down_revision = "0006_agent_persistence"
branch_labels = None
depends_on = None

DECISIONS = "'pending', 'accepted', 'rejected', 'deferred', 'info_requested'"


def upgrade() -> None:
    # The additive DDL works on SQLite and PostgreSQL without copying a tasks
    # table that is already referenced by events and idempotency records.
    op.execute("ALTER TABLE tasks ADD COLUMN reviewer_id VARCHAR(36) REFERENCES subjects (id)")
    op.execute(
        "ALTER TABLE tasks ADD COLUMN decision_state VARCHAR(16) NOT NULL DEFAULT 'pending' "
        f"CONSTRAINT ck_tasks_decision_state CHECK (decision_state IN ({DECISIONS}))"
    )
    op.execute(
        "ALTER TABLE task_events ADD COLUMN decision_state VARCHAR(16) "
        f"CONSTRAINT ck_task_events_decision_state CHECK (decision_state IS NULL OR decision_state IN ({DECISIONS}))"
    )
    op.execute("UPDATE tasks SET decision_state = 'accepted' WHERE state <> 'suggested'")
    op.create_index("ix_tasks_reviewer", "tasks", ["organization_id", "store_id", "reviewer_id"])
    with op.batch_alter_table("task_idempotency", recreate="always") as batch:
        batch.drop_constraint("ck_task_idempotency_operation", type_="check")
        batch.create_check_constraint("ck_task_idempotency_operation",
            "operation IN ('create', 'accept', 'assign', 'start', 'verify', 'close', 'decide')")


def downgrade() -> None:
    connection = op.get_bind()
    # The app rollback is a forward-compatible version switch. Dropping these
    # columns with task history would destroy reviewer and decision evidence.
    for table in (
        "task_idempotency", "task_events", "tasks", "auth_sessions", "employee_accounts",
        "approval_idempotency", "approval_requests", "outbox_events", "audit_events",
        "permission_grants", "role_assignments", "subjects", "stores", "organizations",
    ):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError(f"task policy downgrade refused because {table} contains platform data; use a forward migration or isolated restore")
    with op.batch_alter_table("task_idempotency", recreate="always") as batch:
        batch.drop_constraint("ck_task_idempotency_operation", type_="check")
        batch.create_check_constraint("ck_task_idempotency_operation",
            "operation IN ('create', 'accept', 'assign', 'start', 'verify', 'close')")
    op.drop_index("ix_tasks_reviewer", table_name="tasks")
    op.drop_column("task_events", "decision_state")
    op.drop_column("tasks", "decision_state")
    op.drop_column("tasks", "reviewer_id")
