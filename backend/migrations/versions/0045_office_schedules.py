"""Finite schedule authority and durable suggested-work occurrences.

Explicit schema: no future ORM imports. SQLite task CHECK expansion requires
an isolated migration connection whose FK enforcement is already OFF; this
revision never turns runtime enforcement off. Existing FK violations or task
triggers cause refusal before DDL. Preserve all task columns and rows.
"""
import re

from alembic import op
import sqlalchemy as sa

revision = "0045_office_schedules"
down_revision = "0044_office_exceptions"
branch_labels = None
depends_on = None

TABLES = ("office_schedules", "office_schedule_grants", "office_schedule_occurrences", "office_schedule_receipts")
IMMUTABLE = ("office_schedule_grants", "office_schedule_receipts")


def _scope():
    return [sa.Column("organization_id", sa.String(36), nullable=False),
            sa.Column("store_id", sa.String(36), nullable=False)]


def _id():
    return sa.Column("id", sa.String(36), primary_key=True)


def _string(name, length, nullable=False):
    return sa.Column(name, sa.String(length), nullable=nullable)


def _time(name, nullable=False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _subject(column, name):
    return sa.ForeignKeyConstraint(["organization_id", column], ["subjects.organization_id", "subjects.id"],
                                   name=name, ondelete="RESTRICT")


def _fk(columns, table, targets, name):
    return sa.ForeignKeyConstraint(columns, [table + "." + column for column in targets], name=name, ondelete="RESTRICT")


def _base_constraints(prefix):
    return [sa.UniqueConstraint("organization_id", "store_id", "id", name="uq_" + prefix + "_scope"),
            _fk(["organization_id", "store_id"], "stores", ["organization_id", "id"], "fk_" + prefix + "_store")]


def _task_source_preflight(bind, include_schedule):
    constraints = {c["name"]: c["sqltext"] for c in sa.inspect(bind).get_check_constraints("tasks")}
    actual = constraints.get("ck_tasks_source", "")
    expected = "source IN ('agent', 'human')" if include_schedule else "source IN ('agent', 'human', 'schedule')"
    # PostgreSQL may normalize IN to ANY with explicit text casts. Permit only
    # these exact equivalent spellings, not arbitrary custom CHECK expressions.
    canonical = lambda value: re.sub(r"[\s()]", "", re.sub(r"::(?:character varying|text)(?:\[\])?", "", value.lower()))
    values = "'agent','human'" + ("" if include_schedule else ",'schedule'")
    allowed = {canonical(expected), canonical("source = ANY (ARRAY[" + values + "])")}
    if canonical(actual) not in allowed:
        raise RuntimeError("refuse 0045: unexpected tasks source CHECK; explicit reviewed migration required")
    if bind.dialect.name == "sqlite":
        if bind.exec_driver_sql("PRAGMA foreign_keys").scalar() != 0:
            raise RuntimeError("refuse 0045: task CHECK rebuild requires isolated migration connection with FK already OFF")
        if bind.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
            raise RuntimeError("refuse 0045: existing FK violations; no automatic data repair")
        if bind.execute(sa.text("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name='tasks' LIMIT 1")).first():
            raise RuntimeError("refuse 0045: unknown tasks triggers require explicit preservation plan")
    elif bind.dialect.name != "postgresql":
        raise RuntimeError("refuse 0045: unsupported migration dialect")


def _set_task_source(bind, include_schedule):
    before = bind.execute(sa.text("SELECT count(*) FROM tasks")).scalar_one()
    expression = "source IN ('agent', 'human', 'schedule')" if include_schedule else "source IN ('agent', 'human')"
    if bind.dialect.name == "sqlite":
        # 0007 added this as a column-local CHECK. Reflection promotes it to
        # table level, which would break 0007's native DROP COLUMN on a later
        # empty downgrade. Preserve its original locality in the copied DDL.
        copied = sa.Table("tasks", sa.MetaData(), autoload_with=bind)
        decision_check = next((constraint for constraint in copied.constraints
            if isinstance(constraint, sa.CheckConstraint) and constraint.name == "ck_tasks_decision_state"), None)
        if decision_check is None:
            raise RuntimeError("refuse 0045: missing historical decision-state CHECK")
        copied.constraints.remove(decision_check)
        old_decision = copied.c.decision_state
        if old_decision.primary_key or old_decision.computed is not None or old_decision.foreign_keys:
            raise RuntimeError("refuse 0045: unexpected historical decision column")
        copied.append_column(sa.Column(old_decision.name, old_decision.type,
            sa.CheckConstraint(str(decision_check.sqltext), name=decision_check.name),
            nullable=old_decision.nullable,
            server_default=sa.text(str(old_decision.server_default.arg)) if old_decision.server_default is not None else None),
            replace_existing=True)
        # 0007 also added reviewer_id REFERENCES inline. SQLAlchemy reflection
        # renders it as a table FK; SQLite then refuses 0007's DROP COLUMN.
        # Copy explicitly, retaining column-local CHECK/FK placement as well as
        # every other reflected column, constraint, index and row.
        reviewer_fks = [constraint for constraint in copied.foreign_key_constraints
                        if [column.name for column in constraint.columns] == ["reviewer_id"]]
        if (len(reviewer_fks) != 1 or reviewer_fks[0].name is not None
                or reviewer_fks[0].ondelete is not None
                or [element.target_fullname for element in reviewer_fks[0].elements] != ["subjects.id"]):
            raise RuntimeError("refuse 0045: unexpected historical reviewer FK")
        copied.constraints.remove(reviewer_fks[0])
        source_check = next(constraint for constraint in copied.constraints
                           if isinstance(constraint, sa.CheckConstraint) and constraint.name == "ck_tasks_source")
        source_check.sqltext = sa.text(expression)
        temporary = "_ofc0045_tasks_copy"
        if sa.inspect(bind).has_table(temporary):
            raise RuntimeError("refuse 0045: prior task copy exists; inspect isolated recovery snapshot")
        ddl = str(sa.schema.CreateTable(copied).compile(dialect=bind.dialect))
        ddl, count = re.subn(r"(?m)^(\s*reviewer_id VARCHAR\(36\))(?=[,\s])",
                            r"\1 REFERENCES subjects (id)", ddl)
        if count != 1:
            raise RuntimeError("refuse 0045: reviewer column DDL is not the expected historical shape")
        ddl, count = re.subn(r"^(\s*CREATE TABLE )(?:tasks|\"tasks\")(?=\s*\()",
                            r"\1" + temporary, ddl, count=1)
        if count != 1:
            raise RuntimeError("refuse 0045: unexpected task table DDL")
        bind.exec_driver_sql(ddl)
        names = ", ".join(bind.dialect.identifier_preparer.quote(column.name) for column in copied.columns)
        bind.exec_driver_sql(f"INSERT INTO {temporary} ({names}) SELECT {names} FROM tasks")
        if bind.execute(sa.text(f"SELECT count(*) FROM {temporary}")).scalar_one() != before:
            raise RuntimeError("0045 task copy row count mismatch; original retained")
        bind.exec_driver_sql("DROP TABLE tasks")
        bind.exec_driver_sql(f"ALTER TABLE {temporary} RENAME TO tasks")
        for index in sorted(copied.indexes, key=lambda index: index.name):
            bind.execute(sa.schema.CreateIndex(index))
        if bind.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
            raise RuntimeError("0045 task rebuild failed FK verification; preserve isolated snapshot for recovery")
    else:
        op.drop_constraint("ck_tasks_source", "tasks", type_="check")
        op.create_check_constraint("ck_tasks_source", "tasks", expression)
    if bind.execute(sa.text("SELECT count(*) FROM tasks")).scalar_one() != before:
        raise RuntimeError("0045 task rebuild row count mismatch; preserve isolated snapshot for recovery")


def _immutable_guards(bind):
    for table, message in (("office_schedule_grants", "office schedule grants are immutable"),
                           ("office_schedule_receipts", "office schedule receipts are immutable")):
        if bind.dialect.name == "sqlite":
            for action in ("update", "delete"):
                bind.exec_driver_sql(f"CREATE TRIGGER trg_{table}_{action}_deny BEFORE {action} ON {table} "
                    f"BEGIN SELECT RAISE(ABORT,'{message}'); END")
        else:
            bind.exec_driver_sql(f"CREATE FUNCTION deny_{table}_mutation() RETURNS trigger LANGUAGE plpgsql "
                f"AS $$ BEGIN RAISE EXCEPTION '{message}'; END; $$")
            bind.exec_driver_sql(f"CREATE TRIGGER trg_{table}_deny BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION deny_{table}_mutation()")


def upgrade():
    bind = op.get_bind()
    _task_source_preflight(bind, True)
    _set_task_source(bind, True)
    op.create_table("office_schedules", _id(), *_scope(),
        _string("kind", 24), _string("title", 200), sa.Column("description", sa.Text(), nullable=False),
        sa.Column("rule", sa.JSON(), nullable=False), sa.Column("start_date", sa.Date(), nullable=False),
        _string("reviewer_id", 36), _string("template_digest", 64), _string("state", 24),
        sa.Column("version", sa.Integer(), nullable=False), _string("create_key", 200),
        _string("created_by", 36), _time("created_at"), _time("updated_at"), _time("last_checked_at", True),
        *_base_constraints("os"),
        sa.UniqueConstraint("organization_id", "store_id", "create_key", name="uq_os_create_key"),
        _subject("reviewer_id", "fk_os_reviewer"), _subject("created_by", "fk_os_creator"),
        sa.CheckConstraint("kind IN ('task_template','review_summary')", name="ck_os_kind"),
        sa.CheckConstraint("state IN ('draft','active','paused','needs_reapproval')", name="ck_os_state"),
        sa.CheckConstraint("version > 0", name="ck_os_version_positive"),
        sa.CheckConstraint("length(title) BETWEEN 1 AND 200", name="ck_os_title_length"),
        sa.CheckConstraint("length(description) BETWEEN 1 AND 4000", name="ck_os_description_length"),
        sa.CheckConstraint("length(template_digest) = 64", name="ck_os_template_digest_length"),
        sa.CheckConstraint("length(create_key) BETWEEN 1 AND 200", name="ck_os_create_key_length"),
        sa.CheckConstraint("updated_at >= created_at AND (last_checked_at IS NULL OR last_checked_at >= created_at)", name="ck_os_times"))
    op.create_index("ix_os_state", "office_schedules", ["organization_id", "store_id", "state", "created_at", "id"])
    op.create_table("office_schedule_grants", _id(), *_scope(),
        _string("schedule_id", 36), sa.Column("schedule_version", sa.Integer(), nullable=False),
        _string("template_digest", 64), sa.Column("template_snapshot", sa.JSON(), nullable=False),
        _string("reviewer_id", 36), _string("machine_id", 36),
        _string("approved_by", 36), _string("account_id", 36), sa.Column("account_version", sa.Integer(), nullable=False),
        _string("session_id", 36), sa.Column("authorization_epoch", sa.Integer(), nullable=False),
        _time("valid_from"), _time("expires_at"), sa.Column("max_occurrences", sa.Integer(), nullable=False),
        _string("request_key", 200), _string("input_digest", 64), _time("created_at"),
        *_base_constraints("osg"),
        sa.UniqueConstraint("organization_id", "store_id", "schedule_id", "schedule_version", name="uq_osg_schedule_version"),
        sa.UniqueConstraint("organization_id", "store_id", "schedule_id", "schedule_version", "id", name="uq_osg_scope_binding"),
        sa.UniqueConstraint("organization_id", "store_id", "request_key", name="uq_osg_request_key"),
        _fk(["organization_id", "store_id", "schedule_id"], "office_schedules", ["organization_id", "store_id", "id"], "fk_osg_schedule"),
        _fk(["organization_id", "store_id", "machine_id"], "machine_identities", ["organization_id", "store_id", "id"], "fk_osg_machine"),
        _subject("reviewer_id", "fk_osg_reviewer"), _subject("approved_by", "fk_osg_approver"),
        _fk(["account_id"], "employee_accounts", ["id"], "fk_osg_account"),
        _fk(["session_id"], "auth_sessions", ["id"], "fk_osg_session"),
        sa.CheckConstraint("schedule_version > 0 AND account_version > 0 AND authorization_epoch > 0", name="ck_osg_versions_positive"),
        sa.CheckConstraint("max_occurrences BETWEEN 1 AND 100000", name="ck_osg_max_occurrences"),
        sa.CheckConstraint("length(template_digest) = 64 AND length(input_digest) = 64", name="ck_osg_digests"),
        sa.CheckConstraint("length(request_key) BETWEEN 1 AND 200", name="ck_osg_request_key_length"),
        sa.CheckConstraint("valid_from >= created_at AND expires_at > valid_from", name="ck_osg_times"))
    op.create_index("ix_osg_timeline", "office_schedule_grants", ["organization_id", "store_id", "schedule_id", "created_at", "id"])
    op.create_table("office_schedule_occurrences", _id(), *_scope(),
        _string("schedule_id", 36), sa.Column("schedule_version", sa.Integer(), nullable=False), _string("grant_id", 36),
        sa.Column("local_date", sa.Date(), nullable=False), _string("state", 24), _string("decision_code", 64),
        sa.Column("version", sa.Integer(), nullable=False), _time("created_at"), _time("updated_at"),
        _time("scheduled_at", True), _time("window_end", True), _string("task_id", 36, True), _string("source_digest", 64, True),
        *_base_constraints("oso"),
        sa.UniqueConstraint("organization_id", "store_id", "schedule_id", "local_date", name="uq_oso_slot"),
        sa.UniqueConstraint("organization_id", "store_id", "schedule_id", "schedule_version", "id", name="uq_oso_scope_binding"),
        sa.UniqueConstraint("organization_id", "store_id", "task_id", name="uq_oso_task"),
        _fk(["organization_id", "store_id", "schedule_id"], "office_schedules", ["organization_id", "store_id", "id"], "fk_oso_schedule"),
        _fk(["organization_id", "store_id", "schedule_id", "schedule_version", "grant_id"], "office_schedule_grants", ["organization_id", "store_id", "schedule_id", "schedule_version", "id"], "fk_oso_grant"),
        _fk(["organization_id", "store_id", "task_id"], "tasks", ["organization_id", "store_id", "id"], "fk_oso_task"),
        sa.CheckConstraint("state IN ('queued','materialized','skipped','cancelled','needs_reapproval')", name="ck_oso_state"),
        sa.CheckConstraint("version > 0 AND schedule_version > 0", name="ck_oso_versions_positive"),
        sa.CheckConstraint("length(decision_code) BETWEEN 1 AND 64", name="ck_oso_decision_code_length"),
        sa.CheckConstraint("(scheduled_at IS NULL AND window_end IS NULL) OR (scheduled_at IS NOT NULL AND window_end IS NOT NULL AND window_end > scheduled_at)", name="ck_oso_window_pair"),
        sa.CheckConstraint("source_digest IS NULL OR length(source_digest) = 64", name="ck_oso_source_digest_length"),
        sa.CheckConstraint("updated_at >= created_at", name="ck_oso_times"),
        sa.CheckConstraint("(state = 'materialized' AND task_id IS NOT NULL AND source_digest IS NOT NULL AND scheduled_at IS NOT NULL) OR (state <> 'materialized' AND task_id IS NULL)", name="ck_oso_task_binding"),
        sa.CheckConstraint("state <> 'queued' OR scheduled_at IS NOT NULL", name="ck_oso_queued_window"))
    op.create_index("ix_oso_grant_state", "office_schedule_occurrences", ["organization_id", "store_id", "grant_id", "state"])
    op.create_index("ix_oso_slot", "office_schedule_occurrences", ["organization_id", "store_id", "schedule_id", "local_date"])
    op.create_table("office_schedule_receipts", _id(), *_scope(),
        _string("schedule_id", 36), sa.Column("schedule_version", sa.Integer(), nullable=False),
        _string("operation", 24), _string("request_key", 200), _string("request_digest", 64),
        _string("actor_id", 36), sa.Column("references", sa.JSON(), nullable=False), _time("created_at"),
        _string("occurrence_id", 36, True), _string("grant_id", 36, True), _string("task_id", 36, True),
        *_base_constraints("osr"),
        sa.UniqueConstraint("organization_id", "store_id", "request_key", name="uq_osr_request_key"),
        sa.UniqueConstraint("organization_id", "store_id", "occurrence_id", name="uq_osr_occurrence"),
        _fk(["organization_id", "store_id", "schedule_id"], "office_schedules", ["organization_id", "store_id", "id"], "fk_osr_schedule"),
        _fk(["organization_id", "store_id", "schedule_id", "schedule_version", "occurrence_id"], "office_schedule_occurrences", ["organization_id", "store_id", "schedule_id", "schedule_version", "id"], "fk_osr_occurrence"),
        _fk(["organization_id", "store_id", "schedule_id", "schedule_version", "grant_id"], "office_schedule_grants", ["organization_id", "store_id", "schedule_id", "schedule_version", "id"], "fk_osr_grant"),
        _fk(["organization_id", "store_id", "task_id"], "tasks", ["organization_id", "store_id", "id"], "fk_osr_task"),
        _subject("actor_id", "fk_osr_actor"),
        sa.CheckConstraint("operation IN ('create','replace','approve','pause','materialize','skip','cancel','invalidate','checkpoint')", name="ck_osr_operation"),
        sa.CheckConstraint("schedule_version > 0", name="ck_osr_version_positive"),
        sa.CheckConstraint("length(request_key) BETWEEN 1 AND 200", name="ck_osr_request_key_length"),
        sa.CheckConstraint("length(request_digest) = 64", name="ck_osr_digest_length"),
        sa.CheckConstraint("(operation = 'materialize' AND occurrence_id IS NOT NULL AND grant_id IS NOT NULL AND task_id IS NOT NULL) OR (operation <> 'materialize' AND task_id IS NULL)", name="ck_osr_task_binding"))
    op.create_index("ix_osr_timeline", "office_schedule_receipts", ["organization_id", "store_id", "schedule_id", "created_at", "id"])
    op.create_index("ix_osr_grant_operation", "office_schedule_receipts", ["organization_id", "store_id", "grant_id", "operation"])
    _immutable_guards(bind)


def downgrade():
    bind = op.get_bind()
    protected = (*TABLES, "tasks", "office_exception_cases", "office_exception_receipts", "office_workflows",
                 "office_workflow_steps", "office_workflow_receipts", "erp_sandbox_batches", "erp_sandbox_lines",
                 "erp_sandbox_observations", "scope_security_fences")
    for table in protected:
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError(f"refuse downgrade 0045: {table} contains protected data; retain schema and roll forward")
    _task_source_preflight(bind, False)
    _set_task_source(bind, False)
    for table in reversed(TABLES):
        op.drop_table(table)
        if table in IMMUTABLE and bind.dialect.name == "postgresql":
            bind.exec_driver_sql(f"DROP FUNCTION deny_{table}_mutation()")
