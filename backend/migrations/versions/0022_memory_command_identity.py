"""Forward columns for MEM-001 command identity/versioning (0021 already shipped locally)."""
from alembic import op
import sqlalchemy as sa

revision = "0022_memory_command_identity"
down_revision = "0021_personal_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("personal_memories", sa.Column("idempotency_key", sa.String(200), nullable=True))
    op.add_column("personal_memories", sa.Column("create_digest", sa.String(64), nullable=True))
    op.add_column("personal_memories", sa.Column("persistent", sa.Boolean(), nullable=False,
                                                 server_default=sa.false()))
    op.add_column("personal_memories", sa.Column("correction_key", sa.String(200), nullable=True))
    op.add_column("personal_memories", sa.Column("correction_digest", sa.String(64), nullable=True))
    op.add_column("personal_memories", sa.Column("withdraw_key", sa.String(200), nullable=True))
    with op.batch_alter_table("personal_memories") as batch:
        batch.create_unique_constraint("uq_personal_memories_owner_idem",
                                       ["organization_id", "store_id", "owner_id", "idempotency_key"])


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT COUNT(*) FROM personal_memories")).scalar():
        raise RuntimeError("personal memory command identity downgrade refused while rows exist")
    with op.batch_alter_table("personal_memories") as batch:
        batch.drop_constraint("uq_personal_memories_owner_idem", type_="unique")
        for column in ("withdraw_key", "correction_digest", "correction_key",
                       "persistent", "create_digest", "idempotency_key"):
            batch.drop_column(column)
