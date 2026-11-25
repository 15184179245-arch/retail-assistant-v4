"""ERP-003 isolated synthetic dispatch, line evidence and observations.

No production ERP mapping or credential is added by this migration.
"""
from alembic import op
import sqlalchemy as sa

revision = "0042_erp_sandbox_dispatch"
down_revision = "0041_scope_security_fence"
branch_labels = None
depends_on = None


def _scope():
    return [sa.Column("organization_id", sa.String(36), nullable=False),
            sa.Column("store_id", sa.String(36), nullable=False)]


def upgrade():
    op.create_table("erp_sandbox_batches",
        sa.Column("id", sa.String(36), primary_key=True), *_scope(),
        sa.Column("queue_key", sa.String(200), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("adapter_id", sa.String(40), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("executor_id", sa.String(36), nullable=False),
        sa.Column("account_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("account_version", sa.Integer(), nullable=False),
        sa.Column("authorization_epoch", sa.Integer(), nullable=False),
        sa.Column("authorization_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_erp_sandbox_batch_scope"),
        sa.UniqueConstraint("organization_id", "store_id", "queue_key", name="uq_erp_sandbox_queue_key"),
        sa.ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_erp_sandbox_batch_store", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "executor_id"], ["subjects.organization_id", "subjects.id"], name="fk_erp_sandbox_batch_executor", ondelete="RESTRICT"),
        sa.CheckConstraint("mode = 'synthetic' AND adapter_id = 'synthetic.erp.v1' AND contract_version = 1", name="ck_erp_sandbox_contract"),
        sa.CheckConstraint("state IN ('prepared','dispatched','unknown','partial','sandbox_verified','failed')", name="ck_erp_sandbox_batch_state"),
        sa.CheckConstraint("version > 0 AND account_version > 0 AND authorization_epoch > 0", name="ck_erp_sandbox_batch_versions"),
        sa.CheckConstraint("length(input_digest) = 64 AND length(queue_key) BETWEEN 1 AND 200", name="ck_erp_sandbox_batch_input"),
        sa.CheckConstraint("updated_at >= created_at AND authorization_expires_at > created_at", name="ck_erp_sandbox_batch_times"),
        sa.CheckConstraint("state != 'prepared' OR dispatched_at IS NULL", name="ck_erp_sandbox_not_sent"),
        sa.CheckConstraint("state != 'dispatched' OR dispatched_at IS NOT NULL", name="ck_erp_sandbox_sent"))
    op.create_index("ix_erp_sandbox_batch_state", "erp_sandbox_batches", ["organization_id", "store_id", "state", "created_at"])
    op.create_table("erp_sandbox_lines",
        sa.Column("id", sa.String(36), primary_key=True), *_scope(),
        sa.Column("batch_id", sa.String(36), nullable=False),
        sa.Column("intent_id", sa.String(36), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("target_digest", sa.String(64), nullable=False),
        sa.Column("parameters_digest", sa.String(64), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("approved_parameters", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("external_ref", sa.String(200)),
        sa.Column("receipt_digest", sa.String(64)),
        sa.Column("error_code", sa.String(64)),
        sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_erp_sandbox_line_scope"),
        sa.UniqueConstraint("organization_id", "store_id", "batch_id", "line_no", name="uq_erp_sandbox_line_no"),
        sa.UniqueConstraint("organization_id", "store_id", "intent_id", name="uq_erp_sandbox_intent_once"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "batch_id"], ["erp_sandbox_batches.organization_id", "erp_sandbox_batches.store_id", "erp_sandbox_batches.id"], name="fk_erp_sandbox_line_batch", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "intent_id"], ["erp_command_intents.organization_id", "erp_command_intents.store_id", "erp_command_intents.id"], name="fk_erp_sandbox_line_intent", ondelete="RESTRICT"),
        sa.CheckConstraint("line_no BETWEEN 1 AND 50 AND version > 0", name="ck_erp_sandbox_line_versions"),
        sa.CheckConstraint("state IN ('pending','unknown','sandbox_verified','failed')", name="ck_erp_sandbox_line_state"),
        sa.CheckConstraint("length(request_digest)=64 AND length(target_digest)=64 AND length(parameters_digest)=64 AND length(input_digest)=64", name="ck_erp_sandbox_line_digests"),
        sa.CheckConstraint("receipt_digest IS NULL OR length(receipt_digest)=64", name="ck_erp_sandbox_line_receipt"),
        sa.CheckConstraint("state != 'sandbox_verified' OR (external_ref IS NOT NULL AND length(external_ref)>0 AND receipt_digest IS NOT NULL)", name="ck_erp_sandbox_line_verified"))
    op.create_table("erp_sandbox_observations",
        sa.Column("id", sa.String(36), primary_key=True), *_scope(),
        sa.Column("batch_id", sa.String(36), nullable=False),
        sa.Column("observation_key", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("http_status", sa.Integer()),
        sa.Column("normalized_receipt", sa.JSON()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "store_id", "batch_id", "observation_key", name="uq_erp_sandbox_observation"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "batch_id"], ["erp_sandbox_batches.organization_id", "erp_sandbox_batches.store_id", "erp_sandbox_batches.id"], name="fk_erp_sandbox_observation_batch", ondelete="RESTRICT"),
        sa.CheckConstraint("kind IN ('send_intent','send_error','readback_receipt','readback_error')", name="ck_erp_sandbox_observation_kind"),
        sa.CheckConstraint("length(evidence_digest)=64 AND length(observation_key) BETWEEN 1 AND 200", name="ck_erp_sandbox_observation_input"),
        sa.CheckConstraint("http_status IS NULL OR http_status BETWEEN 100 AND 599", name="ck_erp_sandbox_observation_http"))


def downgrade():
    bind = op.get_bind()
    # Inspect every guard before the first DDL. Existing scopes already carry
    # protected platform data: do not drop even empty new tables on the way to
    # an older revision that would only then refuse the downgrade.
    for table in ("erp_sandbox_observations", "erp_sandbox_lines", "erp_sandbox_batches", "scope_security_fences"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("0042 downgrade refused while sandbox or security-fence data exists; forward-fix")
    op.drop_table("erp_sandbox_observations")
    op.drop_table("erp_sandbox_lines")
    op.drop_index("ix_erp_sandbox_batch_state", table_name="erp_sandbox_batches")
    op.drop_table("erp_sandbox_batches")
