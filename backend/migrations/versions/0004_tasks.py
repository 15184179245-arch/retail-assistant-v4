"""Add scoped collaborative task state, event and idempotency tables.

Revision ID: 0004_tasks
Revises: 0003_approvals
Downgrade is refused while any platform data exists. This preflight runs
before dropping task tables, including when Alembic is asked to reach base.
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_tasks"
down_revision = "0003_approvals"
branch_labels = None
depends_on = None

STATES = "'suggested', 'accepted', 'assigned', 'in_progress', 'verified', 'closed'"


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("proposal_ref", sa.String(200), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="suggested"),
        sa.Column("accepted_by", sa.String(36), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assigned_by", sa.String(36), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assignee_id", sa.String(36), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_by", sa.String(36), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.String(36), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_reason", sa.Text(), nullable=True),
        sa.Column("closed_by", sa.String(36), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_tasks_scope_id"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_tasks_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "created_by"], ["subjects.organization_id", "subjects.id"], name="fk_tasks_creator_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "accepted_by"], ["subjects.organization_id", "subjects.id"], name="fk_tasks_acceptor_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "assigned_by"], ["subjects.organization_id", "subjects.id"], name="fk_tasks_assigner_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "assignee_id"], ["subjects.organization_id", "subjects.id"], name="fk_tasks_assignee_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "started_by"], ["subjects.organization_id", "subjects.id"], name="fk_tasks_starter_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "verified_by"], ["subjects.organization_id", "subjects.id"], name="fk_tasks_verifier_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "closed_by"], ["subjects.organization_id", "subjects.id"], name="fk_tasks_closer_scope", ondelete="RESTRICT"),
        sa.CheckConstraint(f"state IN ({STATES})", name="ck_tasks_state"),
        sa.CheckConstraint("source IN ('agent', 'human')", name="ck_tasks_source"),
        sa.CheckConstraint("length(title) > 0", name="ck_tasks_title_nonempty"),
    )
    op.create_index("ix_tasks_inbox", "tasks", ["organization_id", "store_id", "state", "created_at"])
    op.create_index("ix_tasks_assignee", "tasks", ["organization_id", "store_id", "assignee_id", "state"])

    op.create_table(
        "task_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("from_state", sa.String(16), nullable=True),
        sa.Column("to_state", sa.String(16), nullable=False),
        sa.Column("responsible_id", sa.String(36), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "task_id"], ["tasks.organization_id", "tasks.store_id", "tasks.id"], name="fk_task_events_task_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "actor_id"], ["subjects.organization_id", "subjects.id"], name="fk_task_events_actor_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "responsible_id"], ["subjects.organization_id", "subjects.id"], name="fk_task_events_responsible_scope", ondelete="RESTRICT"),
        sa.CheckConstraint(f"to_state IN ({STATES})", name="ck_task_events_to_state"),
        sa.CheckConstraint(f"from_state IS NULL OR from_state IN ({STATES})", name="ck_task_events_from_state"),
    )
    op.create_index("ix_task_events_timeline", "task_events", ["organization_id", "store_id", "task_id", "occurred_at"])

    op.create_table(
        "task_idempotency",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("organization_id", "store_id", "idempotency_key", name="uq_task_idempotency_scope_key"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_task_idempotency_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "task_id"], ["tasks.organization_id", "tasks.store_id", "tasks.id"], name="fk_task_idempotency_task_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "actor_id"], ["subjects.organization_id", "subjects.id"], name="fk_task_idempotency_actor_scope", ondelete="RESTRICT"),
        sa.CheckConstraint("operation IN ('create', 'accept', 'assign', 'start', 'verify', 'close')", name="ck_task_idempotency_operation"),
    )
    op.create_index("ix_task_idempotency_task", "task_idempotency", ["organization_id", "store_id", "task_id"])


def downgrade() -> None:
    connection = op.get_bind()
    # Alembic may commit each revision separately (notably with SQLite DDL).
    # Check every table present at this revision before the first destructive
    # step, so a head -> base request cannot partly downgrade a populated DB.
    for table in (
        "task_idempotency", "task_events", "tasks",
        "approval_idempotency", "approval_requests",
        "outbox_events", "audit_events",
        "permission_grants", "role_assignments", "subjects", "stores", "organizations",
    ):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError(f"task downgrade refused because {table} contains platform data; restore an isolated backup or use a forward migration")
    op.drop_index("ix_task_idempotency_task", table_name="task_idempotency")
    op.drop_table("task_idempotency")
    op.drop_index("ix_task_events_timeline", table_name="task_events")
    op.drop_table("task_events")
    op.drop_index("ix_tasks_assignee", table_name="tasks")
    op.drop_index("ix_tasks_inbox", table_name="tasks")
    op.drop_table("tasks")
