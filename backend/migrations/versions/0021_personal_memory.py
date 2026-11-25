"""Owner-scoped personal memory (MEM-001)."""
from alembic import op
import sqlalchemy as sa

from backend.app.modules.agents.models import UTCDateTime

revision = "0021_personal_memory"
down_revision = "0020_locations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "personal_memories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("purpose", sa.String(200), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_personal_memories_store_scope", ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "store_id", "id",
                            name="uq_personal_memories_scope_id"),

        sa.CheckConstraint("version > 0", name="ck_personal_memories_version"),
        sa.CheckConstraint("length(content) BETWEEN 1 AND 5000",
                           name="ck_personal_memories_content"),
    )
    op.create_index("ix_personal_memories_owner", "personal_memories",
                    ["organization_id", "store_id", "owner_id", "active"])


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT COUNT(*) FROM personal_memories")).scalar():
        raise RuntimeError("personal memory downgrade refused while rows exist")
    op.drop_index("ix_personal_memories_owner", table_name="personal_memories")
    op.drop_table("personal_memories")
