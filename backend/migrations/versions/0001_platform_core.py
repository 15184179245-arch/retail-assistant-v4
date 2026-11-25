"""Create organization, store, subject, role, and permission foundations.

Revision ID: 0001_platform_core
Revises:
Safe forward strategy: add these new tables only. Downgrade drops only these
new tables and is safe only before dependent production data is written.
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_platform_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
    )
    op.create_table(
        "stores",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("organization_id", "id", name="uq_stores_org_id"),
    )
    op.create_table(
        "subjects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("subject_type", sa.String(24), nullable=False, server_default="person"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("organization_id", "id", name="uq_subjects_org_id"),
    )
    op.create_table(
        "role_assignments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("subject_id", sa.String(36), nullable=False),
        sa.Column("role_key", sa.String(80), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_role_assignments_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "subject_id"], ["subjects.organization_id", "subjects.id"], name="fk_role_assignments_subject_scope", ondelete="RESTRICT"),
    )
    op.create_index("ix_role_assignments_scope", "role_assignments", ["organization_id", "store_id", "subject_id", "active"])
    op.create_table(
        "permission_grants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("permission_key", sa.String(120), nullable=False),
        sa.Column("subject_id", sa.String(36), nullable=True),
        sa.Column("role_key", sa.String(80), nullable=True),
        sa.Column("granted", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_permission_grants_store_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "subject_id"], ["subjects.organization_id", "subjects.id"], name="fk_permission_grants_subject_scope", ondelete="RESTRICT"),
        sa.CheckConstraint("(subject_id IS NOT NULL AND role_key IS NULL) OR (subject_id IS NULL AND role_key IS NOT NULL)", name="ck_permission_grants_exactly_one_selector"),
    )
    op.create_index("ix_permission_grants_scope", "permission_grants", ["organization_id", "store_id", "permission_key", "active"])


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("permission_grants", "role_assignments", "subjects", "stores", "organizations"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("platform core downgrade refused because identity data exists; restore an isolated backup or use a forward migration")
    op.drop_index("ix_permission_grants_scope", table_name="permission_grants")
    op.drop_table("permission_grants")
    op.drop_index("ix_role_assignments_scope", table_name="role_assignments")
    op.drop_table("role_assignments")
    op.drop_table("subjects")
    op.drop_table("stores")
    op.drop_table("organizations")
