"""SKL-003: immutable scoped skill-to-Run pin; old Runs remain unbound.

Revision ID: 0031_skill_run_binding
Revises: 0030_agent_worker_control
"""
from alembic import op
import sqlalchemy as sa
from backend.app.modules.agents.models import UTCDateTime

revision = "0031_skill_run_binding"
down_revision = "0030_agent_worker_control"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_run_skill_bindings",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("skill_id", sa.String(36), nullable=False),
        sa.Column("skill_key", sa.String(120), nullable=False),
        sa.Column("release_tag", sa.String(120), nullable=False),
        sa.Column("spec_sha256", sa.String(64), nullable=False),
        sa.Column("evaluation_digest", sa.String(64), nullable=False),
        sa.Column("skill_version", sa.Integer(), nullable=False),
        sa.Column("provider_key", sa.String(120), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("prompt_version", sa.String(120), nullable=False),
        sa.Column("toolset_version", sa.String(120), nullable=False),
        sa.Column("role_key", sa.String(40), nullable=False),
        sa.Column("specialty", sa.String(40), nullable=False),
        sa.Column("role_spec_sha256", sa.String(64), nullable=False),
        sa.Column("tool_contract_sha256", sa.String(64), nullable=False),
        sa.Column("allowed_tools", sa.JSON(), nullable=False),
        sa.Column("selection_reason", sa.String(120), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "store_id", "run_id"],
            ["agent_runs.organization_id", "agent_runs.store_id", "agent_runs.id"],
            name="fk_skill_run_binding_run", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["organization_id", "store_id", "skill_id"],
            ["agent_skill_versions.organization_id", "agent_skill_versions.store_id",
             "agent_skill_versions.id"],
            name="fk_skill_run_binding_skill", ondelete="RESTRICT"),
        sa.CheckConstraint("skill_version > 0", name="ck_skill_run_binding_version"),
        sa.CheckConstraint(
            "length(spec_sha256) = 64 AND length(evaluation_digest) = 64 "
            "AND length(role_spec_sha256) = 64 AND length(tool_contract_sha256) = 64", name="ck_skill_run_binding_sha"),
        sa.CheckConstraint("role_key = 'specialist' AND "
                           "specialty IN ('catalog','knowledge','tasks')",
                           name="ck_skill_run_binding_role"),
    )
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(sa.text("""
            CREATE TRIGGER trg_skill_run_binding_no_update
            BEFORE UPDATE ON agent_run_skill_bindings
            BEGIN SELECT RAISE(ABORT, 'run skill binding is immutable'); END
        """))
        op.execute(sa.text("""
            CREATE TRIGGER trg_skill_run_binding_no_delete
            BEFORE DELETE ON agent_run_skill_bindings
            BEGIN SELECT RAISE(ABORT, 'run skill binding is immutable'); END
        """))
    elif bind.dialect.name == "postgresql":
        op.execute(sa.text("""
            CREATE FUNCTION deny_skill_run_binding_mutation() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'run skill binding is immutable'; END;
            $$
        """))
        op.execute(sa.text("""
            CREATE TRIGGER trg_skill_run_binding_immutable
            BEFORE UPDATE OR DELETE ON agent_run_skill_bindings
            FOR EACH ROW EXECUTE FUNCTION deny_skill_run_binding_mutation()
        """))


def downgrade():
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM agent_run_skill_bindings LIMIT 1")).first():
        raise RuntimeError("0031 skill run downgrade refused with immutable records; forward-fix")
    op.drop_table("agent_run_skill_bindings")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("DROP FUNCTION deny_skill_run_binding_mutation()"))
