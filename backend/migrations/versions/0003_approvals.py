"""Create scoped approval requests and idempotency ledger.

Revision ID: 0003_approvals
Revises: 0002_audit_outbox
Downgrade is permitted only while all tables present at this revision are empty.
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_approvals"
down_revision = "0002_audit_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("requester_id", sa.String(36), nullable=False),
        sa.Column("subject_type", sa.String(100), nullable=False),
        sa.Column("subject_id", sa.String(200), nullable=False),
        sa.Column("action_key", sa.String(120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="requested"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(36), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("cancelled_by", sa.String(36), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column("expired_by", sa.String(36), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_approval_requests_scope_id"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_approval_requests_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "requester_id"], ["subjects.organization_id", "subjects.id"], name="fk_approval_requests_requester_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "decided_by"], ["subjects.organization_id", "subjects.id"], name="fk_approval_requests_decider_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "cancelled_by"], ["subjects.organization_id", "subjects.id"], name="fk_approval_requests_canceller_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "expired_by"], ["subjects.organization_id", "subjects.id"], name="fk_approval_requests_expirer_scope", ondelete="RESTRICT"),
        sa.CheckConstraint("state IN ('requested', 'approved', 'rejected', 'cancelled', 'expired')", name="ck_approval_requests_state"),
        sa.CheckConstraint("length(subject_type) > 0 AND length(subject_id) > 0 AND length(action_key) > 0", name="ck_approval_requests_subject_action_nonempty"),
    )
    op.create_index("ix_approval_requests_inbox", "approval_requests", ["organization_id", "store_id", "state", "requested_at"])

    op.create_table(
        "approval_idempotency",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("approval_id", sa.String(36), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("organization_id", "store_id", "idempotency_key", name="uq_approval_idempotency_scope_key"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_approval_idempotency_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "approval_id"], ["approval_requests.organization_id", "approval_requests.store_id", "approval_requests.id"], name="fk_approval_idempotency_request_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "actor_id"], ["subjects.organization_id", "subjects.id"], name="fk_approval_idempotency_actor_scope", ondelete="RESTRICT"),
        sa.CheckConstraint("operation IN ('request', 'decision', 'cancel', 'expire')", name="ck_approval_idempotency_operation"),
    )
    op.create_index("ix_approval_idempotency_approval", "approval_idempotency", ["organization_id", "store_id", "approval_id"])


def downgrade() -> None:
    connection = op.get_bind()
    for table in (
        "approval_idempotency", "approval_requests", "outbox_events", "audit_events",
        "permission_grants", "role_assignments", "subjects", "stores", "organizations",
    ):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError(f"approval downgrade refused because {table} contains platform data; restore an isolated backup or use a forward migration")
    op.drop_index("ix_approval_idempotency_approval", table_name="approval_idempotency")
    op.drop_table("approval_idempotency")
    op.drop_index("ix_approval_requests_inbox", table_name="approval_requests")
    op.drop_table("approval_requests")
