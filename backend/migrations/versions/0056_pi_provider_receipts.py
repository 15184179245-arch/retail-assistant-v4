"""AGT-044 H1 additive one-shot provider facts; never backfill/replay."""
from alembic import op
import sqlalchemy as sa

revision = "0056_pi_provider_receipts"
down_revision = "0055_pi_native_tool_receipts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("agent_pi_provider_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("model_step_id", sa.String(36), nullable=False),
        sa.Column("model_kind", sa.String(16), nullable=False),
        sa.Column("epoch", sa.Integer, nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("permit_digest", sa.String(64), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("toolset_version", sa.String(120), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("wire_protocol", sa.String(40), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("send_started", sa.Boolean, nullable=True),
        sa.Column("provider_request_id", sa.String(128), nullable=True),
        sa.Column("response_digest", sa.String(64), nullable=True),
        sa.Column("usage_input", sa.BigInteger, nullable=True),
        sa.Column("usage_output", sa.BigInteger, nullable=True),
        sa.Column("cache_read", sa.BigInteger, nullable=True),
        sa.Column("cache_write", sa.BigInteger, nullable=True),
        sa.Column("error_code", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id","store_id","run_id","model_step_id","model_kind"],
            ["agent_steps.organization_id","agent_steps.store_id","agent_steps.run_id","agent_steps.id","agent_steps.kind"],
            name="fk_pi_provider_model", ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id","store_id","run_id","model_step_id", name="uq_pi_provider_step"),
        sa.UniqueConstraint("permit_digest", name="uq_pi_provider_permit"),
        sa.UniqueConstraint("organization_id","store_id","run_id","request_id", name="uq_pi_provider_request"),
        sa.CheckConstraint("model_kind = 'model' AND epoch > 0 AND model = 'deepseek-v4.1-flash' "
            "AND wire_protocol = 'v4.pi-readonly/2'", name="ck_pi_provider_identity"),
        sa.CheckConstraint("length(permit_digest) = 64 AND length(input_digest) = 64 AND length(request_digest) = 64 "
            "AND length(manifest_digest) = 64 AND length(request_id) > 0 AND length(toolset_version) > 0 "
            "AND (response_digest IS NULL OR length(response_digest) = 64)", name="ck_pi_provider_digests"),
        sa.CheckConstraint("(state IN ('permitted','send_intent') AND response_digest IS NULL AND error_code IS NULL "
            "AND usage_input IS NULL AND usage_output IS NULL AND cache_read IS NULL AND cache_write IS NULL) "
            "OR (state = 'responded' AND response_digest IS NOT NULL AND error_code IS NULL AND send_started = true "
            "AND usage_input IS NOT NULL AND usage_output IS NOT NULL AND cache_read IS NOT NULL AND cache_write = 0) "
            "OR (state IN ('unknown','not_sent') AND error_code IS NOT NULL)", name="ck_pi_provider_state"),
        sa.CheckConstraint("(usage_input IS NULL OR usage_input >= 0) AND (usage_output IS NULL OR usage_output >= 0) "
            "AND (cache_read IS NULL OR cache_read >= 0) AND (cache_write IS NULL OR cache_write >= 0)", name="ck_pi_provider_usage"),
        sa.CheckConstraint("updated_at >= created_at", name="ck_pi_provider_times"),
    )


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError("pi_provider_downgrade_requires_verified_empty_table")
    if op.get_bind().execute(sa.text("SELECT 1 FROM agent_pi_provider_receipts LIMIT 1")).first() is not None:
        raise RuntimeError("pi_provider_facts_require_safe_forward_recovery")
    op.drop_table("agent_pi_provider_receipts")
