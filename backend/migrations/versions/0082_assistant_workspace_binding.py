"""Nullable Workspace pins on existing Work authorities, no backfill/re-signing."""
from alembic import op
import sqlalchemy as sa

revision = "0082_assistant_workspace_binding"
down_revision = "0081_merge_retained_sales"
branch_labels = depends_on = None

COLUMNS = (
    ("assistant_work", "workspace_context"),
    ("assistant_work_authorities", "workspace_binding_sha256"),
    ("assistant_turn_authorities", "workspace_binding_sha256"),
)


def _apply(bind, context):
    if set(context.get_current_heads()) != {down_revision}:
        raise RuntimeError("workspace_binding_exact_predecessor_required")
    # Refuse partial/manual installation before the first DDL, never infer a
    # completed revision from a few column names or alter version rows.
    inspector = sa.inspect(bind)
    for table, name in COLUMNS:
        if name in {column["name"] for column in inspector.get_columns(table)}:
            raise RuntimeError("workspace_binding_partial_schema_refused")
    op.add_column("assistant_work",
        sa.Column("workspace_context", sa.JSON(none_as_null=True), nullable=True))
    for table, check in (
        ("assistant_work_authorities", "ck_work_authority_workspace_pin"),
        ("assistant_turn_authorities", "ck_turn_authority_workspace_pin"),
    ):
        op.add_column(table, sa.Column("workspace_binding_sha256", sa.String(64),
            sa.CheckConstraint("workspace_binding_sha256 IS NULL OR length(workspace_binding_sha256) = 64",
                               name=check), nullable=True))


def upgrade():
    context = op.get_context()
    if context.as_sql:
        raise RuntimeError("workspace_binding_requires_live_preflight")
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect not in ("sqlite", "postgresql"):
        raise RuntimeError("workspace_binding_unsupported_dialect")
    root = bind.get_transaction()
    if (context.connection is not bind or context._in_external_transaction
            or root is None or not root.is_active or context._transaction is not root
            or bind.in_nested_transaction()):
        raise RuntimeError("workspace_binding_owned_proxy_required")
    if dialect == "sqlite":
        if context.impl.transactional_ddl is not False:
            raise RuntimeError("workspace_binding_sqlite_proxy_required")
        from backend.migrations.pi_result_forward import retained_step_errors as owned_step_errors
        with owned_step_errors(bind):
            raw = bind.connection.driver_connection
            if raw.in_transaction:
                raise RuntimeError("workspace_binding_external_physical_transaction")
            raw.execute("PRAGMA foreign_keys=ON").close()
            if raw.execute("PRAGMA foreign_keys").fetchone() != (1,):
                raise RuntimeError("workspace_binding_fk_required")
            raw.execute("BEGIN IMMEDIATE").close()
            _apply(bind, context)
    else:
        # Only the isolated additive slice is portable. The complete formal PG
        # history remains blocked by its explicit0079/retained-Sales gates.
        if context.impl.transactional_ddl is not True:
            raise RuntimeError("workspace_binding_pg_transaction_required")
        _apply(bind, context)


def downgrade():
    raise RuntimeError("workspace_binding_forward_only_disable_feature_preserve_pins")
