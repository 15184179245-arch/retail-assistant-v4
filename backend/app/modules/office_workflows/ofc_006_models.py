"""OFC-006 B1: store-scoped office workflow instances, steps, and immutable receipts.

Structure only. These models never commit, authorize, hash, or call a domain
service: capability checks, digests, resume logic, and every state transition
belong to the B2 service. A row here records what a human or the fixed flow
declared, never that a business action succeeded, that an approval is still
valid, or that a formal ERP document exists.

``references`` is a bounded normalized-reference placeholder (ids, versions,
hashes only); the B2 service validates its shape. Never store originals,
credentials, arbitrary tool arguments, prices, or quantities in these tables.

The receipt table is append-only in both SQLite and PostgreSQL: every DML guard
below is installed by ``create_all`` fixtures; the hand-written 0043 migration
mirrors the emitted DDL without importing future ORM definitions.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint, ForeignKeyConstraint, Index, Integer, JSON, String,
    UniqueConstraint, event,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.modules.pricing.models import UTCDateTime

WORKFLOW_STATES = ("active", "paused", "needs_human", "failed", "closed")
STEP_STATES = ("pending", "completed", "failed", "unknown")
STEP_KINDS = ("intake", "extraction", "review", "approval", "execution",
              "verification", "closure")
RECEIPT_OPERATIONS = ("create", "advance", "pause", "resume", "fail", "mark_unknown")

RECEIPT_IMMUTABLE_MESSAGE = "office workflow receipts are immutable"

_STEP_KIND_MAP = " OR ".join(
    f"(step_no = {number} AND kind = '{kind}')"
    for number, kind in enumerate(STEP_KINDS, start=1)
)

_WORKFLOW_STATE_CHECK = "state IN (" + ", ".join(f"'{s}'" for s in WORKFLOW_STATES) + ")"
_STEP_STATE_CHECK = "state IN (" + ", ".join(f"'{s}'" for s in STEP_STATES) + ")"
_OPERATION_CHECK = "operation IN (" + ", ".join(f"'{o}'" for o in RECEIPT_OPERATIONS) + ")"


class OfficeWorkflow(Base):
    """One fixed ``material_review.v1`` flow instance bound to one human Task."""

    __tablename__ = "office_workflows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    template_key: Mapped[str] = mapped_column(String(40), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    task_context_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_evidence_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    rule_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    create_key: Mapped[str] = mapped_column(String(200), nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    next_step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id",
                         name="uq_office_workflow_scope"),
        UniqueConstraint("organization_id", "store_id", "task_id",
                         name="uq_office_workflow_task"),
        UniqueConstraint("organization_id", "store_id", "create_key",
                         name="uq_office_workflow_create_key"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_office_workflow_store", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "task_id"],
                             ["tasks.organization_id", "tasks.store_id", "tasks.id"],
                             name="fk_office_workflow_task", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "source_evidence_id"],
                             ["office_evidence.organization_id", "office_evidence.store_id",
                              "office_evidence.id"],
                             name="fk_office_workflow_evidence", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "rule_id"],
                             ["office_rule_versions.organization_id",
                              "office_rule_versions.store_id", "office_rule_versions.id"],
                             name="fk_office_workflow_rule", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "origin_run_id"],
                             ["agent_runs.organization_id", "agent_runs.store_id",
                              "agent_runs.id"],
                             name="fk_office_workflow_origin_run", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "created_by"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_office_workflow_creator", ondelete="RESTRICT"),
        CheckConstraint("template_key = 'material_review.v1'",
                        name="ck_office_workflow_template_key"),
        CheckConstraint("template_version = 1",
                        name="ck_office_workflow_template_version"),
        CheckConstraint("execution_mode = 'manual_task'",
                        name="ck_office_workflow_execution_mode"),
        CheckConstraint(_WORKFLOW_STATE_CHECK, name="ck_office_workflow_state"),
        CheckConstraint("version > 0", name="ck_office_workflow_version_positive"),
        CheckConstraint("next_step_no BETWEEN 1 AND 8",
                        name="ck_office_workflow_next_step"),
        CheckConstraint("length(task_context_digest) = 64 "
                        "AND length(source_evidence_digest) = 64 "
                        "AND length(input_digest) = 64",
                        name="ck_office_workflow_digests"),
        CheckConstraint("(rule_id IS NULL AND rule_digest IS NULL) "
                        "OR (rule_id IS NOT NULL AND rule_digest IS NOT NULL "
                        "AND length(rule_digest) = 64)",
                        name="ck_office_workflow_rule_pair"),
        CheckConstraint("length(create_key) BETWEEN 1 AND 200",
                        name="ck_office_workflow_create_key_length"),
        CheckConstraint("updated_at >= created_at",
                        name="ck_office_workflow_times"),
        # Closed iff the flow finished all seven steps and recorded closure;
        # every other state stays before closure and carries no closed_at.
        CheckConstraint("(state = 'closed' AND next_step_no = 8 "
                        "AND closed_at IS NOT NULL) "
                        "OR (state <> 'closed' AND next_step_no <= 7 "
                        "AND closed_at IS NULL)",
                        name="ck_office_workflow_closed"),
        Index("ix_office_workflow_scope_state", "organization_id", "store_id",
              "state", "updated_at", "id"),
    )


class OfficeWorkflowStep(Base):
    """One of the seven fixed steps; step_no and kind are structurally paired."""

    __tablename__ = "office_workflow_steps"

    organization_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    store_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    step_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    responsible_id: Mapped[str] = mapped_column(String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id", "workflow_id"],
                             ["office_workflows.organization_id",
                              "office_workflows.store_id", "office_workflows.id"],
                             name="fk_office_workflow_step_parent", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "responsible_id"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_office_workflow_step_responsible",
                             ondelete="RESTRICT"),
        CheckConstraint("step_no BETWEEN 1 AND 7",
                        name="ck_office_workflow_step_no"),
        CheckConstraint(_STEP_KIND_MAP, name="ck_office_workflow_step_kind_map"),
        CheckConstraint(_STEP_STATE_CHECK, name="ck_office_workflow_step_state"),
        CheckConstraint("version > 0",
                        name="ck_office_workflow_step_version_positive"),
        CheckConstraint("(input_digest IS NULL OR length(input_digest) = 64) "
                        "AND (output_digest IS NULL OR length(output_digest) = 64)",
                        name="ck_office_workflow_step_digests"),
        # A completed step proves both digests and the closure time; no other
        # state may carry a completion time (failures/unknown stay open).
        CheckConstraint("(state = 'completed' AND input_digest IS NOT NULL "
                        "AND output_digest IS NOT NULL AND completed_at IS NOT NULL) "
                        "OR (state <> 'completed' AND completed_at IS NULL)",
                        name="ck_office_workflow_step_completion"),
        Index("ix_office_workflow_step_responsible", "organization_id", "store_id",
              "responsible_id", "state"),
    )


class OfficeWorkflowReceipt(Base):
    """Append-only record of one workflow command; UPDATE/DELETE are denied in DB."""

    __tablename__ = "office_workflow_receipts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(36), nullable=False)
    step_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    workflow_version: Mapped[int] = mapped_column(Integer, nullable=False)
    request_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    output_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    references: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id",
                         name="uq_office_workflow_receipt_scope"),
        UniqueConstraint("organization_id", "store_id", "workflow_id",
                         "workflow_version",
                         name="uq_office_workflow_receipt_version"),
        UniqueConstraint("organization_id", "store_id", "request_key",
                         name="uq_office_workflow_receipt_request"),
        ForeignKeyConstraint(["organization_id", "store_id", "workflow_id"],
                             ["office_workflows.organization_id",
                              "office_workflows.store_id", "office_workflows.id"],
                             name="fk_office_workflow_receipt_parent",
                             ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "workflow_id", "step_no"],
                             ["office_workflow_steps.organization_id",
                              "office_workflow_steps.store_id",
                              "office_workflow_steps.workflow_id",
                              "office_workflow_steps.step_no"],
                             name="fk_office_workflow_receipt_step",
                             ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "actor_id"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_office_workflow_receipt_actor",
                             ondelete="RESTRICT"),
        CheckConstraint(_OPERATION_CHECK, name="ck_office_workflow_receipt_operation"),
        CheckConstraint("workflow_version > 0",
                        name="ck_office_workflow_receipt_version_positive"),
        CheckConstraint("length(request_key) BETWEEN 1 AND 200",
                        name="ck_office_workflow_receipt_request_key_length"),
        CheckConstraint("length(request_digest) = 64 AND length(input_digest) = 64 "
                        "AND length(output_digest) = 64",
                        name="ck_office_workflow_receipt_digests"),
        # Parent-level commands carry no step; an advance must name one real step.
        # step_no IS NOT NULL is explicit: in SQL a NULL comparison makes the
        # whole CHECK NULL, which SQLite treats as satisfied (an advance without
        # a step would otherwise slip through).
        CheckConstraint("(operation = 'advance' AND step_no IS NOT NULL "
                        "AND step_no BETWEEN 1 AND 7) "
                        "OR (operation <> 'advance' AND step_no IS NULL)",
                        name="ck_office_workflow_receipt_step_binding"),
        Index("ix_office_workflow_receipt_timeline", "organization_id", "store_id",
              "workflow_id", "created_at", "id"),
    )


def receipt_immutable_guard_statements(table_name: str, dialect: str) -> list[str]:
    """DDL that denies UPDATE/DELETE on the receipt table for one dialect."""
    if dialect == "sqlite":
        return [
            f"CREATE TRIGGER trg_{table_name}_{action}_deny "
            f"BEFORE {action} ON {table_name} "
            f"BEGIN SELECT RAISE(ABORT,'{RECEIPT_IMMUTABLE_MESSAGE}'); END"
            for action in ("update", "delete")
        ]
    if dialect == "postgresql":
        function = f"deny_{table_name}_mutation"
        return [
            f"CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql "
            f"AS $$ BEGIN RAISE EXCEPTION '{RECEIPT_IMMUTABLE_MESSAGE}'; END; $$",
            f"CREATE TRIGGER trg_{table_name}_deny BEFORE UPDATE OR DELETE "
            f"ON {table_name} FOR EACH ROW EXECUTE FUNCTION {function}()",
        ]
    return []


def install_immutable_receipt_guards(target, connection, **_kw) -> None:
    """Install the real DML guards (SQLAlchemy ``after_create`` listener).

    ``target`` may be the Table or its name, so the 0043 migration can call
    ``install_immutable_receipt_guards("office_workflow_receipts", op.get_bind())``
    without importing the model here.
    """
    table_name = target if isinstance(target, str) else target.name
    for statement in receipt_immutable_guard_statements(table_name,
                                                         connection.dialect.name):
        connection.exec_driver_sql(statement)


event.listen(OfficeWorkflowReceipt.__table__, "after_create",
             install_immutable_receipt_guards)
