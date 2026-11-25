"""OFC-008 B1b: store-scoped schedule, immutable grant, occurrence, and immutable receipt.

Structure only. These tables never authorize, dispatch, hash, or call a domain
service, and every row needs explicit values (no column defaults here). A row
records what a human or a fixed worker declared; it is never evidence that a
grant is still valid, that an occurrence was authorized, that quota remains, or
that a Task, Outbox delivery, or upstream business action succeeded.

``rule`` and ``references`` are bounded normalized JSON placeholders (ids,
versions, hashes, timestamps, small counters, fixed reason codes); the service
validates their shape and must never store originals, credentials, arbitrary
tool arguments, URLs, scripts, prices, or quantities.

Grants and receipts are append-only in both SQLite and PostgreSQL: the DML
guards below are installed by ``create_all`` fixtures and a hand-written
migration mirrors the emitted DDL without importing these ORM definitions.
Consumed quota is counted from immutable materialize receipts, never from a
mutable counter on the grant.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint, Date, ForeignKeyConstraint, Index, Integer, JSON, String,
    Text, UniqueConstraint, event,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.modules.pricing.models import UTCDateTime

SCHEDULE_KINDS = ("task_template", "review_summary")
SCHEDULE_STATES = ("draft", "active", "paused", "needs_reapproval")
OCCURRENCE_STATES = ("queued", "materialized", "skipped", "cancelled",
                     "needs_reapproval")
RECEIPT_OPERATIONS = ("create", "replace", "approve", "pause", "materialize", "skip",
                      "cancel", "invalidate", "checkpoint")

GRANT_IMMUTABLE_MESSAGE = "office schedule grants are immutable"
RECEIPT_IMMUTABLE_MESSAGE = "office schedule receipts are immutable"

_GRANT_TABLE = "office_schedule_grants"
_RECEIPT_TABLE = "office_schedule_receipts"


def _in(values: tuple[str, ...]) -> str:
    return " IN (" + ", ".join(f"'{value}'" for value in values) + ")"


class OfficeSchedule(Base):
    """One explicit calendar rule; edits bump ``version`` and keep history."""

    __tablename__ = "office_schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    rule: Mapped[dict] = mapped_column(JSON, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    reviewer_id: Mapped[str] = mapped_column(String(36), nullable=False)
    template_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    create_key: Mapped[str] = mapped_column(String(200), nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(),
                                                            nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id",
                         name="uq_os_scope"),
        UniqueConstraint("organization_id", "store_id", "create_key",
                         name="uq_os_create_key"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_os_store", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "reviewer_id"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_os_reviewer", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "created_by"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_os_creator", ondelete="RESTRICT"),
        CheckConstraint("kind" + _in(SCHEDULE_KINDS), name="ck_os_kind"),
        CheckConstraint("state" + _in(SCHEDULE_STATES), name="ck_os_state"),
        CheckConstraint("version > 0", name="ck_os_version_positive"),
        CheckConstraint("length(title) BETWEEN 1 AND 200",
                        name="ck_os_title_length"),
        CheckConstraint("length(description) BETWEEN 1 AND 4000",
                        name="ck_os_description_length"),
        CheckConstraint("length(template_digest) = 64",
                        name="ck_os_template_digest_length"),
        CheckConstraint("length(create_key) BETWEEN 1 AND 200",
                        name="ck_os_create_key_length"),
        CheckConstraint("updated_at >= created_at AND "
                        "(last_checked_at IS NULL OR last_checked_at >= created_at)",
                        name="ck_os_times"),
        Index("ix_os_state", "organization_id", "store_id", "state",
              "created_at", "id"),
    )


class OfficeScheduleGrant(Base):
    """Immutable approval evidence for one schedule version; quota is not stored."""

    __tablename__ = _GRANT_TABLE

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    schedule_id: Mapped[str] = mapped_column(String(36), nullable=False)
    schedule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    template_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    template_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    reviewer_id: Mapped[str] = mapped_column(String(36), nullable=False)
    machine_id: Mapped[str] = mapped_column(String(36), nullable=False)
    approved_by: Mapped[str] = mapped_column(String(36), nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_version: Mapped[int] = mapped_column(Integer, nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    authorization_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    max_occurrences: Mapped[int] = mapped_column(Integer, nullable=False)
    request_key: Mapped[str] = mapped_column(String(200), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_osg_scope"),
        UniqueConstraint("organization_id", "store_id", "schedule_id",
                         "schedule_version", name="uq_osg_schedule_version"),
        UniqueConstraint("organization_id", "store_id", "schedule_id",
                         "schedule_version", "id", name="uq_osg_scope_binding"),
        UniqueConstraint("organization_id", "store_id", "request_key",
                         name="uq_osg_request_key"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_osg_store", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "schedule_id"],
                             ["office_schedules.organization_id",
                              "office_schedules.store_id", "office_schedules.id"],
                             name="fk_osg_schedule", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "machine_id"],
                             ["machine_identities.organization_id",
                              "machine_identities.store_id",
                              "machine_identities.id"],
                             name="fk_osg_machine", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "reviewer_id"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_osg_reviewer", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "approved_by"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_osg_approver", ondelete="RESTRICT"),
        ForeignKeyConstraint(["account_id"], ["employee_accounts.id"],
                             name="fk_osg_account", ondelete="RESTRICT"),
        ForeignKeyConstraint(["session_id"], ["auth_sessions.id"],
                             name="fk_osg_session", ondelete="RESTRICT"),
        CheckConstraint("schedule_version > 0 AND account_version > 0 "
                        "AND authorization_epoch > 0",
                        name="ck_osg_versions_positive"),
        CheckConstraint("max_occurrences BETWEEN 1 AND 100000",
                        name="ck_osg_max_occurrences"),
        CheckConstraint("length(template_digest) = 64 AND length(input_digest) = 64",
                        name="ck_osg_digests"),
        CheckConstraint("length(request_key) BETWEEN 1 AND 200",
                        name="ck_osg_request_key_length"),
        CheckConstraint("valid_from >= created_at AND expires_at > valid_from",
                        name="ck_osg_times"),
        Index("ix_osg_timeline", "organization_id", "store_id", "schedule_id",
              "created_at", "id"),
    )


class OfficeScheduleOccurrence(Base):
    """One local-day slot; uniqueness excludes schedule version and grant."""

    __tablename__ = "office_schedule_occurrences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    schedule_id: Mapped[str] = mapped_column(String(36), nullable=False)
    schedule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    grant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    decision_code: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(),
                                                          nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(UTCDateTime(),
                                                        nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_oso_scope"),
        UniqueConstraint("organization_id", "store_id", "schedule_id",
                         "local_date", name="uq_oso_slot"),
        UniqueConstraint("organization_id", "store_id", "schedule_id",
                         "schedule_version", "id", name="uq_oso_scope_binding"),
        UniqueConstraint("organization_id", "store_id", "task_id",
                         name="uq_oso_task"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_oso_store", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "schedule_id"],
                             ["office_schedules.organization_id",
                              "office_schedules.store_id", "office_schedules.id"],
                             name="fk_oso_schedule", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "schedule_id",
                              "schedule_version", "grant_id"],
                             ["office_schedule_grants.organization_id",
                              "office_schedule_grants.store_id",
                              "office_schedule_grants.schedule_id",
                              "office_schedule_grants.schedule_version",
                              "office_schedule_grants.id"],
                             name="fk_oso_grant", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "task_id"],
                             ["tasks.organization_id", "tasks.store_id", "tasks.id"],
                             name="fk_oso_task", ondelete="RESTRICT"),
        CheckConstraint("state" + _in(OCCURRENCE_STATES), name="ck_oso_state"),
        CheckConstraint("version > 0 AND schedule_version > 0",
                        name="ck_oso_versions_positive"),
        CheckConstraint("length(decision_code) BETWEEN 1 AND 64",
                        name="ck_oso_decision_code_length"),
        CheckConstraint("(scheduled_at IS NULL AND window_end IS NULL) "
                        "OR (scheduled_at IS NOT NULL AND window_end IS NOT NULL "
                        "AND window_end > scheduled_at)",
                        name="ck_oso_window_pair"),
        CheckConstraint("source_digest IS NULL OR length(source_digest) = 64",
                        name="ck_oso_source_digest_length"),
        CheckConstraint("updated_at >= created_at", name="ck_oso_times"),
        # A materialized slot proves its Task and source digest; every other
        # state carries no Task. NULL comparison needs explicit IS NOT NULL.
        CheckConstraint("(state = 'materialized' AND task_id IS NOT NULL "
                        "AND source_digest IS NOT NULL AND scheduled_at IS NOT NULL) "
                        "OR (state <> 'materialized' AND task_id IS NULL)",
                        name="ck_oso_task_binding"),
        CheckConstraint("state <> 'queued' OR scheduled_at IS NOT NULL",
                        name="ck_oso_queued_window"),
        Index("ix_oso_grant_state", "organization_id", "store_id", "grant_id",
              "state"),
        Index("ix_oso_slot", "organization_id", "store_id", "schedule_id",
              "local_date"),
    )


class OfficeScheduleReceipt(Base):
    """Append-only record of one schedule command; UPDATE/DELETE are denied in DB."""

    __tablename__ = _RECEIPT_TABLE

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    schedule_id: Mapped[str] = mapped_column(String(36), nullable=False)
    schedule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(String(24), nullable=False)
    request_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    references: Mapped[dict] = mapped_column(JSON, nullable=False)
    occurrence_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    grant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_osr_scope"),
        UniqueConstraint("organization_id", "store_id", "request_key",
                         name="uq_osr_request_key"),
        UniqueConstraint("organization_id", "store_id", "occurrence_id",
                         name="uq_osr_occurrence"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_osr_store", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "schedule_id"],
                             ["office_schedules.organization_id",
                              "office_schedules.store_id", "office_schedules.id"],
                             name="fk_osr_schedule", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "schedule_id",
                              "schedule_version", "occurrence_id"],
                             ["office_schedule_occurrences.organization_id",
                              "office_schedule_occurrences.store_id",
                              "office_schedule_occurrences.schedule_id",
                              "office_schedule_occurrences.schedule_version",
                              "office_schedule_occurrences.id"],
                             name="fk_osr_occurrence", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "schedule_id",
                              "schedule_version", "grant_id"],
                             ["office_schedule_grants.organization_id",
                              "office_schedule_grants.store_id",
                              "office_schedule_grants.schedule_id",
                              "office_schedule_grants.schedule_version",
                              "office_schedule_grants.id"],
                             name="fk_osr_grant", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "task_id"],
                             ["tasks.organization_id", "tasks.store_id", "tasks.id"],
                             name="fk_osr_task", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "actor_id"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_osr_actor", ondelete="RESTRICT"),
        CheckConstraint("operation" + _in(RECEIPT_OPERATIONS),
                        name="ck_osr_operation"),
        CheckConstraint("schedule_version > 0",
                        name="ck_osr_version_positive"),
        CheckConstraint("length(request_key) BETWEEN 1 AND 200",
                        name="ck_osr_request_key_length"),
        CheckConstraint("length(request_digest) = 64",
                        name="ck_osr_digest_length"),
        # Generation binds occurrence, grant, and Task together; management
        # commands carry no Task. NULL needs explicit IS NULL/IS NOT NULL.
        CheckConstraint("(operation = 'materialize' AND occurrence_id IS NOT NULL "
                        "AND grant_id IS NOT NULL AND task_id IS NOT NULL) "
                        "OR (operation <> 'materialize' AND task_id IS NULL)",
                        name="ck_osr_task_binding"),
        Index("ix_osr_timeline", "organization_id", "store_id", "schedule_id",
              "created_at", "id"),
        Index("ix_osr_grant_operation", "organization_id", "store_id", "grant_id",
              "operation"),
    )


def immutable_guard_statements(table_name: str, dialect: str,
                               message: str) -> list[str]:
    """DDL that denies UPDATE/DELETE on one append-only schedule table."""
    if dialect == "sqlite":
        return [
            f"CREATE TRIGGER trg_{table_name}_{action}_deny "
            f"BEFORE {action} ON {table_name} "
            f"BEGIN SELECT RAISE(ABORT,'{message}'); END"
            for action in ("update", "delete")
        ]
    if dialect == "postgresql":
        function = f"deny_{table_name}_mutation"
        return [
            f"CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql "
            f"AS $$ BEGIN RAISE EXCEPTION '{message}'; END; $$",
            f"CREATE TRIGGER trg_{table_name}_deny BEFORE UPDATE OR DELETE "
            f"ON {table_name} FOR EACH ROW EXECUTE FUNCTION {function}()",
        ]
    return []


def _table_name(target) -> str:
    if hasattr(target, "__tablename__"):
        return target.__tablename__
    if isinstance(target, str):
        return target
    return target.name


def install_immutable_guards(target, connection, **_kw) -> None:
    """Install real DML guards (SQLAlchemy ``after_create`` listener)."""
    table_name = _table_name(target)
    if table_name == _GRANT_TABLE:
        message = GRANT_IMMUTABLE_MESSAGE
    elif table_name == _RECEIPT_TABLE:
        message = RECEIPT_IMMUTABLE_MESSAGE
    else:
        raise ValueError(f"unknown schedule append-only table: {table_name}")
    for statement in immutable_guard_statements(table_name,
                                                connection.dialect.name, message):
        connection.exec_driver_sql(statement)


event.listen(OfficeScheduleGrant.__table__, "after_create", install_immutable_guards)
event.listen(OfficeScheduleReceipt.__table__, "after_create", install_immutable_guards)
