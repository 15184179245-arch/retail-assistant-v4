"""Add scoped audit and transactional outbox tables.

Revision ID: 0002_audit_outbox
Revises: 0001_platform_core

Audit append-only is enforced by the application service interface in this slice.
No database triggers are installed: SQLite and PostgreSQL trigger DDL/behavior
would need separate verified migrations before claiming database immutability.
Downgrade is refused once any table present at this revision contains data.
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_audit_outbox"
down_revision = "0001_platform_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(120), nullable=False),
        sa.Column("target_type", sa.String(120), nullable=False),
        sa.Column("target_id", sa.String(200), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_audit_events_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "actor_id"], ["subjects.organization_id", "subjects.id"], name="fk_audit_events_actor_scope", ondelete="RESTRICT"),
    )
    op.create_index("ix_audit_events_scope_time", "audit_events", ["organization_id", "store_id", "occurred_at"])
    op.create_index("uq_audit_events_idempotency", "audit_events", ["organization_id", "store_id", "idempotency_key"], unique=True)

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("event_type", sa.String(120), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(120), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("external_receipt", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_outbox_events_store_scope", ondelete="RESTRICT"),
        sa.CheckConstraint("status IN ('pending', 'leased', 'retry', 'dead', 'delivered')", name="ck_outbox_events_status"),
        sa.CheckConstraint("attempts >= 0", name="ck_outbox_events_attempts_nonnegative"),
    )
    op.create_index("uq_outbox_scope_idempotency", "outbox_events", ["organization_id", "store_id", "idempotency_key"], unique=True)
    op.create_index("ix_outbox_claim", "outbox_events", ["status", "available_at", "lease_until"])


def downgrade() -> None:
    connection = op.get_bind()
    for table in (
        "outbox_events", "audit_events", "permission_grants", "role_assignments",
        "subjects", "stores", "organizations",
    ):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError(f"audit/outbox downgrade refused because {table} contains platform data; restore an isolated backup or use a forward migration")
    op.drop_index("ix_outbox_claim", table_name="outbox_events")
    op.drop_index("uq_outbox_scope_idempotency", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index("uq_audit_events_idempotency", table_name="audit_events")
    op.drop_index("ix_audit_events_scope_time", table_name="audit_events")
    op.drop_table("audit_events")
