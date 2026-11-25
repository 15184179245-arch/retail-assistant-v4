"""OPS-005: per-store machine identities with versioned, revocable token hashes."""
from alembic import op
import sqlalchemy as sa

revision = "0028_machine_credentials"
down_revision = "0027_notifications"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "machine_identities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("subject_id", sa.String(36), nullable=False),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_machine_id_scope"),
        sa.UniqueConstraint("organization_id", "store_id", "label", name="uq_machine_label_scope"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_machine_store", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "subject_id"], ["subjects.organization_id", "subjects.id"], name="fk_machine_subject", ondelete="RESTRICT"),
        sa.CheckConstraint("purpose IN ('worker','pos_plugin')", name="ck_machine_purpose"),
        sa.CheckConstraint("version >= 1", name="ck_machine_version"),
    )
    op.create_table(
        "machine_credentials",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("machine_id", sa.String(36), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("issued_by", sa.String(36), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_machine_credentials_token_hash"),
        sa.UniqueConstraint("machine_id", "version", name="uq_machine_credential_version"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "machine_id"],
                                ["machine_identities.organization_id", "machine_identities.store_id", "machine_identities.id"],
                                name="fk_machine_credential_identity", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "issued_by"], ["subjects.organization_id", "subjects.id"],
                                name="fk_machine_credential_issuer", ondelete="RESTRICT"),
        sa.CheckConstraint("version >= 1", name="ck_machine_credential_version"),
        sa.CheckConstraint("expires_at > issued_at", name="ck_machine_credential_dates"),
    )
    op.create_index("ix_machine_credential_scope", "machine_credentials",
                    ["organization_id", "store_id", "machine_id", "revoked_at"])


def downgrade():
    bind = op.get_bind()
    for table in ("machine_credentials", "machine_identities"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("0028 machine credentials downgrade refused with data; forward-fix")
    op.drop_index("ix_machine_credential_scope", table_name="machine_credentials")
    op.drop_table("machine_credentials")
    op.drop_table("machine_identities")
