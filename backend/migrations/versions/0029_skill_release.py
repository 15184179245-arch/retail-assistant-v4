"""SKL-001: immutable scoped skill bindings, review history and one active version."""
from alembic import op
import sqlalchemy as sa

from backend.app.modules.agents.models import UTCDateTime

revision = "0029_skill_release"
down_revision = "0028_machine_credentials"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_skill_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("skill_key", sa.String(120), nullable=False),
        sa.Column("release_tag", sa.String(120), nullable=False),
        sa.Column("spec_sha256", sa.String(64), nullable=False),
        sa.Column("provider_key", sa.String(120), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("prompt_version", sa.String(120), nullable=False),
        sa.Column("toolset_version", sa.String(120), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("evaluated_by", sa.String(36), nullable=True),
        sa.Column("approved_by", sa.String(36), nullable=True),
        sa.Column("activated_by", sa.String(36), nullable=True),
        sa.Column("evaluation_digest", sa.String(64), nullable=True),
        sa.Column("evaluation_snapshot", sa.JSON(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_agent_skill_scope_id"),
        sa.UniqueConstraint("organization_id", "store_id", "skill_key", "release_tag",
                            name="uq_agent_skill_release"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"],
                                ["stores.organization_id", "stores.id"],
                                name="fk_agent_skill_store", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "created_by"],
                                ["subjects.organization_id", "subjects.id"],
                                name="fk_agent_skill_creator", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "evaluated_by"],
                                ["subjects.organization_id", "subjects.id"],
                                name="fk_agent_skill_evaluator", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "approved_by"],
                                ["subjects.organization_id", "subjects.id"],
                                name="fk_agent_skill_approver", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "activated_by"],
                                ["subjects.organization_id", "subjects.id"],
                                name="fk_agent_skill_activator", ondelete="RESTRICT"),
        sa.CheckConstraint("state IN ('draft','evaluated','approved','active','rolled_back')",
                           name="ck_agent_skill_state"),
        sa.CheckConstraint("version >= 1", name="ck_agent_skill_version"),
        sa.CheckConstraint("length(spec_sha256) = 64", name="ck_agent_skill_spec_sha256"),
        sa.CheckConstraint("evaluation_digest IS NULL OR length(evaluation_digest) = 64",
                           name="ck_agent_skill_evaluation_sha256"),
        sa.CheckConstraint("length(skill_key) > 0 AND length(release_tag) > 0 AND "
                           "length(provider_key) > 0 AND length(model) > 0 AND "
                           "length(prompt_version) > 0 AND length(toolset_version) > 0",
                           name="ck_agent_skill_binding_nonempty"),
        sa.CheckConstraint("updated_at >= created_at", name="ck_agent_skill_updated_after_created"),
    )
    op.create_index("ix_agent_skill_scope", "agent_skill_versions",
                    ["organization_id", "store_id", "skill_key", "state"])
    op.create_index("uq_agent_skill_one_active", "agent_skill_versions",
                    ["organization_id", "store_id", "skill_key"], unique=True,
                    sqlite_where=sa.text("state = 'active'"),
                    postgresql_where=sa.text("state = 'active'"))


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM agent_skill_versions LIMIT 1")).first() is not None:
        raise RuntimeError("0029 skill release downgrade refused with versions; forward-fix")
    op.drop_index("uq_agent_skill_one_active", table_name="agent_skill_versions")
    op.drop_index("ix_agent_skill_scope", table_name="agent_skill_versions")
    op.drop_table("agent_skill_versions")
