"""OFC-004 store-scoped immutable policy proposals and human decision receipts.

Never use these rows as pricing/ERP master data. A rule is actionable only
after a separate human-approved decision and a fresh authorization check.
"""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    CheckConstraint, DDL, ForeignKeyConstraint, Index, Integer, JSON, String,
    UniqueConstraint, event,
)
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.db.base import Base
from backend.app.modules.pricing.models import UTCDateTime


class OfficeRuleVersion(Base):
    __tablename__ = "office_rule_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    rule_key: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(200), nullable=False)
    rule_body: Mapped[dict] = mapped_column(JSON, nullable=False)
    source_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    submitted_by: Mapped[str] = mapped_column(String(36), nullable=False)
    submit_key: Mapped[str] = mapped_column(String(200), nullable=False)
    submit_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id",
                         name="uq_office_rule_scope"),
        UniqueConstraint("organization_id", "store_id", "rule_key", "version",
                         name="uq_office_rule_version"),
        UniqueConstraint("organization_id", "store_id", "submit_key",
                         name="uq_office_rule_submit_key"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_office_rule_store", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "submitted_by"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_office_rule_submitter", ondelete="RESTRICT"),
        CheckConstraint("version > 0", name="ck_office_rule_version"),
        CheckConstraint("kind IN ('customer_mapping','supplier_mapping',"
                        "'package_conversion','return_policy','exchange_policy')",
                        name="ck_office_rule_kind"),
        CheckConstraint("length(source_sha256) = 64 AND length(body_sha256) = 64 "
                        "AND length(submit_digest) = 64", name="ck_office_rule_digests"),
        CheckConstraint("valid_until IS NULL OR valid_until > valid_from",
                        name="ck_office_rule_window"),
        Index("ix_office_rule_lookup", "organization_id", "store_id",
              "kind", "scope_key", "valid_from"),
    )


class OfficeRuleDecision(Base):
    __tablename__ = "office_rule_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(36), nullable=False)
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    decision_version: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(36), nullable=False)
    request_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "rule_id", "decision_version",
                         name="uq_office_rule_decision_sequence"),
        UniqueConstraint("organization_id", "store_id", "request_key",
                         name="uq_office_rule_decision_request"),
        ForeignKeyConstraint(["organization_id", "store_id", "rule_id"],
                             ["office_rule_versions.organization_id",
                              "office_rule_versions.store_id", "office_rule_versions.id"],
                             name="fk_office_rule_decision_rule", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "decided_by"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_office_rule_decider", ondelete="RESTRICT"),
        CheckConstraint("decision_version IN (1,2)",
                        name="ck_office_rule_decision_version"),
        CheckConstraint("decision IN ('approved','rejected','revoked')",
                        name="ck_office_rule_decision_enum"),
        CheckConstraint("length(input_sha256) = 64 AND length(request_digest) = 64",
                        name="ck_office_rule_decision_digest"),
        Index("ix_office_rule_decisions_latest", "organization_id", "store_id",
              "rule_id", "decision_version"),
    )


def _install_immutable_guards(table, connection, **_kw):
    """Mirror Alembic triggers for isolated Base.metadata.create_all fixtures."""
    name = table.name
    dialect = connection.dialect.name
    if dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            connection.exec_driver_sql(
                f"CREATE TRIGGER trg_{name}_{action.lower()}_deny "
                f"BEFORE {action} ON {name} "
                "BEGIN SELECT RAISE(ABORT,'office rule records are immutable'); END"
            )
    elif dialect == "postgresql":
        fname = f"deny_{name}_mutation"
        connection.exec_driver_sql(
            f"CREATE FUNCTION {fname}() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'office rule records are immutable'; END; $$"
        )
        connection.exec_driver_sql(
            f"CREATE TRIGGER trg_{name}_deny BEFORE UPDATE OR DELETE ON {name} "
            f"FOR EACH ROW EXECUTE FUNCTION {fname}()"
        )


for _table in (OfficeRuleVersion.__table__, OfficeRuleDecision.__table__):
    event.listen(_table, "after_create", _install_immutable_guards)
