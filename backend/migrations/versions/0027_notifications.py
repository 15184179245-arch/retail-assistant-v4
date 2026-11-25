"""NOT-001 scoped in-app notifications; no outbound delivery or auto-escalation."""
from alembic import op
import sqlalchemy as sa

from backend.app.modules.agents.models import UTCDateTime

revision = "0027_notifications"
down_revision = "0026_stocktake_observations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("recipient_id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("source_event_key", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("rule_version", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("read_at", UTCDateTime(), nullable=True),
        sa.UniqueConstraint("organization_id", "store_id", "id",
                            name="uq_notifications_scope_id"),
        sa.UniqueConstraint("organization_id", "store_id", "recipient_id",
                            "source_event_key", "kind",
                            name="uq_notifications_event_recipient"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_notifications_store", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "recipient_id"],
                                ["subjects.organization_id", "subjects.id"],
                                name="fk_notifications_recipient", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "task_id"],
                                ["tasks.organization_id", "tasks.store_id", "tasks.id"],
                                name="fk_notifications_task", ondelete="RESTRICT"),
        sa.CheckConstraint("kind IN ('review_requested','assigned','overdue','manual_check','failure')",
                           name="ck_notifications_kind"),
        sa.CheckConstraint("version > 0", name="ck_notifications_version"),
    )
    op.create_index("ix_notifications_inbox", "notifications",
                    ["organization_id", "store_id", "recipient_id", "created_at"])
    op.create_table(
        "notification_commands",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("notification_id", sa.String(36), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "actor_id",
                            "idempotency_key", name="uq_notification_commands_actor_key"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_notification_commands_store", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "actor_id"],
                                ["subjects.organization_id", "subjects.id"],
                                name="fk_notification_commands_actor", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "notification_id"],
                                ["notifications.organization_id", "notifications.store_id", "notifications.id"],
                                name="fk_notification_commands_notification", ondelete="RESTRICT"),
    )


def downgrade():
    bind = op.get_bind()
    for table in ("notification_commands", "notifications"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("0027 notifications downgrade refused with data; forward-fix")
    op.drop_table("notification_commands")
    op.drop_index("ix_notifications_inbox", table_name="notifications")
    op.drop_table("notifications")
