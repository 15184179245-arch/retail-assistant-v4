"""Add employee accounts and opaque login sessions.

Revision ID: 0005_employee_auth
Revises: 0004_tasks
Downgrade is refused while any platform table contains data.
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_employee_auth"
down_revision = "0004_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "employee_accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("subject_id", sa.String(36), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(200), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("username", name="uq_employee_accounts_username"),
        sa.UniqueConstraint("organization_id", "store_id", "subject_id", name="uq_employee_accounts_store_subject"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_employee_accounts_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "subject_id"], ["subjects.organization_id", "subjects.id"], name="fk_employee_accounts_subject_scope", ondelete="RESTRICT"),
        sa.CheckConstraint("version >= 1", name="ck_employee_accounts_version_positive"),
        sa.CheckConstraint("failed_attempts >= 0", name="ck_employee_accounts_failed_attempts_nonnegative"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("employee_accounts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("account_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
        sa.CheckConstraint("account_version >= 1", name="ck_auth_sessions_account_version_positive"),
    )
    op.create_index("ix_auth_sessions_account", "auth_sessions", ["account_id"])


def downgrade() -> None:
    connection = op.get_bind()
    # Check every table at this revision before dropping anything. Alembic can
    # commit DDL between revisions, so a populated database must fail atomically
    # at this revision rather than lose only the auth tables.
    for table in (
        "auth_sessions", "employee_accounts", "task_idempotency", "task_events", "tasks",
        "approval_idempotency", "approval_requests", "outbox_events", "audit_events",
        "permission_grants", "role_assignments", "subjects", "stores", "organizations",
    ):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError(f"employee auth downgrade refused because {table} contains platform data; restore an isolated backup or use a forward migration")
    op.drop_index("ix_auth_sessions_account", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("employee_accounts")
