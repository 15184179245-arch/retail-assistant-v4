"""OFC-007 B1: store-scoped exception cases and immutable handover receipts.

Structure only. These tables never authorize, hash, claim, or call a domain
service: capability checks, source re-reads, clocks, digests, and every state
transition belong to the B2 service. A row here records that a human or the
queue declared something, never that the source business problem is solved, that
the receiving batch was booked, that an ERP document exists, or that an agent
handled an exception.

``source_id`` is an exact pointer: it must equal the single non-null typed
foreign key named by ``source_type``, so the six sources stay store-scoped FKs
instead of a polymorphic string reference. ``source_version`` is nullable on
purpose: Tasks, ERP intents and sales handoffs have no integer version, and B1
does not invent one; the frozen contract uses a real content/event-set digest in
``initial_source_digest`` for those sources.

``references`` is a bounded normalized-reference placeholder (ids, versions,
hashes only); the B2 service validates its shape. Never store originals,
credentials, prices, quantities or arbitrary tool arguments here.

The receipt table is append-only in both SQLite and PostgreSQL: the DML guards
below are installed by ``create_all`` fixtures; the hand-written migration
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

CASE_STATES = ("open", "owned", "transfer_pending", "resolved")
CASE_KINDS = (
    "shortage", "overage", "wrong_item", "identity_ambiguous", "partial",
    "unknown_write", "overdue", "price_change", "workflow_attention",
)
# Six real sources only; a source the frozen contract does not name cannot be
# claimed by client text.
SOURCE_TYPES = (
    "receiving_discrepancy", "erp_sandbox", "erp_intent",
    "sales_handoff", "workflow", "task",
)
RECEIPT_OPERATIONS = (
    "create", "claim", "takeover", "propose_transfer", "accept_transfer",
    "cancel_transfer", "resolve", "reopen",
)

RECEIPT_TABLE = "office_exception_receipts"
RECEIPT_IMMUTABLE_MESSAGE = "office exception receipts are immutable"

# (source_type, pointer column, referenced table). Every referenced table
# exposes a scope-unique key (organization_id, store_id, id), so each FK is
# store-scoped and a cross-store pointer fails at the database boundary.
SOURCE_POINTERS = (
    ("receiving_discrepancy", "receiving_discrepancy_id", "receiving_discrepancies"),
    ("erp_sandbox", "erp_sandbox_batch_id", "erp_sandbox_batches"),
    ("erp_intent", "erp_intent_id", "erp_command_intents"),
    ("sales_handoff", "sales_handoff_id", "sales_handoffs"),
    ("workflow", "workflow_id", "office_workflows"),
    ("task", "task_id", "tasks"),
)
_POINTERS = tuple(pointer for _source, pointer, _table in SOURCE_POINTERS)

# Only the kind each real source can raise. ``receiving`` keeps all five of its
# own discrepancy kinds (including price_change, which is outside the seven
# core kinds); the other sources are deliberately narrow.
_KIND_MAP = " OR ".join((
    "(source_type = 'receiving_discrepancy' AND kind IN "
    "('shortage', 'overage', 'wrong_item', 'identity_ambiguous', 'price_change'))",
    "(source_type = 'erp_sandbox' AND kind IN ('partial', 'unknown_write'))",
    "(source_type = 'erp_intent' AND kind = 'unknown_write')",
    "(source_type = 'sales_handoff' AND kind = 'unknown_write')",
    "(source_type = 'workflow' AND kind = 'workflow_attention')",
    "(source_type = 'task' AND kind = 'overdue')",
))

# Exactly one pointer is non-null, it belongs to source_type, and source_id
# names that same row. Without the explicit non-equality branches a NULL
# comparison would make SQL treat the whole CHECK as satisfied.
_SELECTOR_MAP = " OR ".join(
    "(" + " AND ".join((
        f"source_type = '{source}'",
        f"{pointer} IS NOT NULL",
        f"source_id = {pointer}",
        *(f"{other} IS NULL" for other in _POINTERS if other != pointer),
    )) + ")"
    for source, pointer, _table in SOURCE_POINTERS
)

# Open has no owner and no closure; owned has an owner but nothing pending;
# transfer_pending keeps the old owner responsible while another person is
# proposed; resolved requires a real closure time and proof digest, and cannot
# precede case creation.
_STATE_SHAPE = " OR ".join((
    "(state = 'open' AND owner_id IS NULL AND pending_owner_id IS NULL "
    "AND resolved_at IS NULL AND resolution_digest IS NULL)",
    "(state = 'owned' AND owner_id IS NOT NULL AND pending_owner_id IS NULL "
    "AND resolved_at IS NULL AND resolution_digest IS NULL)",
    "(state = 'transfer_pending' AND owner_id IS NOT NULL "
    "AND pending_owner_id IS NOT NULL AND pending_owner_id <> owner_id "
    "AND resolved_at IS NULL AND resolution_digest IS NULL)",
    "(state = 'resolved' AND owner_id IS NOT NULL AND pending_owner_id IS NULL "
    "AND resolved_at IS NOT NULL AND resolution_digest IS NOT NULL "
    "AND resolved_at >= created_at)",
))

_STATE_CHECK = "state IN (" + ", ".join(f"'{s}'" for s in CASE_STATES) + ")"
_OPERATION_CHECK = "operation IN (" + ", ".join(f"'{o}'" for o in RECEIPT_OPERATIONS) + ")"

_SOURCE_FKS = tuple(
    ForeignKeyConstraint(
        ["organization_id", "store_id", pointer],
        [f"{table}.organization_id", f"{table}.store_id", f"{table}.id"],
        name=f"fk_office_exception_case_{source}",
        ondelete="RESTRICT",
    )
    for source, pointer, table in SOURCE_POINTERS
)


class OfficeExceptionCase(Base):
    """One tracked problem on one real source, owned by at most one human."""

    __tablename__ = "office_exception_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source_id: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    receiving_discrepancy_id: Mapped[str | None] = mapped_column(String(36))
    erp_sandbox_batch_id: Mapped[str | None] = mapped_column(String(36))
    erp_intent_id: Mapped[str | None] = mapped_column(String(36))
    sales_handoff_id: Mapped[str | None] = mapped_column(String(36))
    workflow_id: Mapped[str | None] = mapped_column(String(36))
    task_id: Mapped[str | None] = mapped_column(String(36))
    initial_source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_id: Mapped[str | None] = mapped_column(String(36))
    pending_owner_id: Mapped[str | None] = mapped_column(String(36))
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    resolution_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    create_key: Mapped[str] = mapped_column(String(200), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id",
                         name="uq_office_exception_case_scope"),
        # One tracked case per real source problem; a recurrence reopens this
        # row (explicit reopen receipt) instead of hiding history in a new one.
        UniqueConstraint("organization_id", "store_id", "source_type",
                         "source_id", "kind",
                         name="uq_office_exception_case_source"),
        UniqueConstraint("organization_id", "store_id", "create_key",
                         name="uq_office_exception_case_create_key"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_office_exception_case_store",
                             ondelete="RESTRICT"),
        *_SOURCE_FKS,
        ForeignKeyConstraint(["organization_id", "owner_id"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_office_exception_case_owner",
                             ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "pending_owner_id"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_office_exception_case_pending_owner",
                             ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "created_by"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_office_exception_case_creator",
                             ondelete="RESTRICT"),
        CheckConstraint(_SELECTOR_MAP, name="ck_office_exception_case_selector"),
        CheckConstraint(_KIND_MAP, name="ck_office_exception_case_kind_map"),
        CheckConstraint(_STATE_CHECK, name="ck_office_exception_case_state"),
        CheckConstraint("version > 0",
                        name="ck_office_exception_case_version_positive"),
        CheckConstraint("source_version IS NULL OR source_version > 0",
                        name="ck_office_exception_case_source_version"),
        CheckConstraint("length(initial_source_digest) = 64 "
                        "AND (resolution_digest IS NULL "
                        "OR length(resolution_digest) = 64)",
                        name="ck_office_exception_case_digests"),
        CheckConstraint(_STATE_SHAPE, name="ck_office_exception_case_state_shape"),
        CheckConstraint("updated_at >= created_at",
                        name="ck_office_exception_case_times"),
        CheckConstraint("length(create_key) BETWEEN 1 AND 200",
                        name="ck_office_exception_case_create_key_length"),
        Index("ix_office_exception_case_queue", "organization_id", "store_id",
              "state", "created_at", "id"),
        Index("ix_office_exception_case_owner", "organization_id", "store_id",
              "owner_id", "state"),
    )


class OfficeExceptionReceipt(Base):
    """Append-only record of one case command; UPDATE/DELETE are denied in DB."""

    __tablename__ = RECEIPT_TABLE

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    case_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation: Mapped[str] = mapped_column(String(24), nullable=False)
    case_version: Mapped[int] = mapped_column(Integer, nullable=False)
    request_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    from_owner_id: Mapped[str | None] = mapped_column(String(36))
    to_owner_id: Mapped[str | None] = mapped_column(String(36))
    resolution_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    references: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id",
                         name="uq_office_exception_receipt_scope"),
        UniqueConstraint("organization_id", "store_id", "case_id", "case_version",
                         name="uq_office_exception_receipt_version"),
        UniqueConstraint("organization_id", "store_id", "request_key",
                         name="uq_office_exception_receipt_request"),
        ForeignKeyConstraint(["organization_id", "store_id", "case_id"],
                             ["office_exception_cases.organization_id",
                              "office_exception_cases.store_id",
                              "office_exception_cases.id"],
                             name="fk_office_exception_receipt_parent",
                             ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "actor_id"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_office_exception_receipt_actor",
                             ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "from_owner_id"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_office_exception_receipt_from_owner",
                             ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "to_owner_id"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_office_exception_receipt_to_owner",
                             ondelete="RESTRICT"),
        CheckConstraint(_OPERATION_CHECK,
                        name="ck_office_exception_receipt_operation"),
        CheckConstraint("case_version > 0",
                        name="ck_office_exception_receipt_version_positive"),
        CheckConstraint("length(request_key) BETWEEN 1 AND 200",
                        name="ck_office_exception_receipt_request_key_length"),
        CheckConstraint("length(request_digest) = 64 AND length(source_digest) = 64 "
                        "AND length(reason_digest) = 64 "
                        "AND (resolution_digest IS NULL "
                        "OR length(resolution_digest) = 64)",
                        name="ck_office_exception_receipt_digests"),
        # Only resolution carries proof that something was verified; every other
        # command must stay free of it. NULL comparison is spelled out so a
        # missing digest cannot pass as "unknown".
        CheckConstraint("(operation = 'resolve' AND resolution_digest IS NOT NULL) "
                        "OR (operation <> 'resolve' AND resolution_digest IS NULL)",
                        name="ck_office_exception_receipt_resolution"),
        Index("ix_office_exception_receipt_timeline", "organization_id",
              "store_id", "case_id", "case_version"),
    )


def exception_receipt_guard_statements(dialect: str,
                                       table_name: str = RECEIPT_TABLE) -> list[str]:
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


def exception_receipt_guard_drop_statements(dialect: str,
                                            table_name: str = RECEIPT_TABLE) -> list[str]:
    """Reverse of ``exception_receipt_guard_statements`` for the downgrade."""
    if dialect == "sqlite":
        return [f"DROP TRIGGER IF EXISTS trg_{table_name}_{action}_deny"
                for action in ("update", "delete")]
    if dialect == "postgresql":
        return [
            f"DROP TRIGGER IF EXISTS trg_{table_name}_deny ON {table_name}",
            f"DROP FUNCTION IF EXISTS deny_{table_name}_mutation()",
        ]
    return []


def install_exception_receipt_guards(target, connection, **_kw) -> None:
    """Install the real DML guards (SQLAlchemy ``after_create`` listener).

    ``target`` may be the Table or its name, so the migration can call
    ``install_exception_receipt_guards("office_exception_receipts", op.get_bind())``
    without importing this module.
    """
    table_name = target if isinstance(target, str) else target.name
    for statement in exception_receipt_guard_statements(
            connection.dialect.name, table_name):
        connection.exec_driver_sql(statement)


event.listen(OfficeExceptionReceipt.__table__, "after_create",
             install_exception_receipt_guards)
