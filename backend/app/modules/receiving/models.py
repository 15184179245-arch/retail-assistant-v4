"""PUR-002: scoped receiving observations, NOT formal receipts or ERP stock."""
from datetime import datetime

from sqlalchemy import CheckConstraint, DDL, ForeignKeyConstraint, Index, Integer, JSON, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime
from backend.app.modules.purchasing import models as purchasing_models  # noqa: F401
from backend.app.modules.identity import models as identity_models  # noqa: F401

KINDS = ("shortage", "overage", "wrong_item", "price_change", "identity_unknown")


class ReceivingBatch(Base):
    __tablename__ = "receiving_batches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    intent_id: Mapped[str | None] = mapped_column(String(36))
    expected_intent_version: Mapped[int | None] = mapped_column(Integer())
    delivery_note_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer(), nullable=False)
    confirmed_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_receiving_batches_scope"),
        UniqueConstraint("organization_id", "store_id", "delivery_note_sha256", name="uq_receiving_note_once"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                             name="fk_receiving_batches_store", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "intent_id"],
                             ["purchase_intents.organization_id", "purchase_intents.store_id",
                              "purchase_intents.id"], name="fk_receiving_batch_intent", ondelete="RESTRICT"),
        CheckConstraint("(intent_id IS NULL AND expected_intent_version IS NULL) OR "
                        "(intent_id IS NOT NULL AND expected_intent_version > 0)",
                        name="ck_receiving_batch_intent_version"),
        CheckConstraint("status IN ('observed','reviewed') AND version > 0",
                        name="ck_receiving_batch_state"),
        CheckConstraint("length(delivery_note_sha256)=64", name="ck_receiving_batch_evidence"),
        CheckConstraint("(status='observed' AND confirmed_by IS NULL AND confirmed_at IS NULL) OR "
                        "(status='reviewed' AND confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",
                        name="ck_receiving_batch_confirmation"),
        Index("ix_receiving_batches_scope_time", "organization_id", "store_id", "created_at"),
    )


class ReceivingLine(Base):
    __tablename__ = "receiving_lines"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False)
    line_key: Mapped[str] = mapped_column(String(80), nullable=False)
    intent_line_id: Mapped[str | None] = mapped_column(String(36))
    product_id: Mapped[str | None] = mapped_column(String(36))
    product_version: Mapped[int | None] = mapped_column(Integer())
    unknown_product_label: Mapped[str | None] = mapped_column(String(200))
    observed_quantity: Mapped[str] = mapped_column(String(40), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    final_for_intent_line: Mapped[bool] = mapped_column(default=False, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    quoted_price: Mapped[str | None] = mapped_column(String(40))
    delivered_price: Mapped[str | None] = mapped_column(String(40))
    price_evidence_sha256: Mapped[str | None] = mapped_column(String(64))
    observed_by: Mapped[str] = mapped_column(String(36), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_receiving_lines_scope"),
        UniqueConstraint("organization_id", "store_id", "batch_id", "line_key",
                         name="uq_receiving_batch_line_key"),
        ForeignKeyConstraint(["organization_id", "store_id", "batch_id"],
                             ["receiving_batches.organization_id", "receiving_batches.store_id",
                              "receiving_batches.id"], name="fk_receiving_line_batch", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "intent_line_id"],
                             ["purchase_intent_lines.organization_id", "purchase_intent_lines.store_id",
                              "purchase_intent_lines.id"], name="fk_receiving_line_intent", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "product_id"],
                             ["catalog_products.organization_id", "catalog_products.store_id",
                              "catalog_products.id"], name="fk_receiving_line_product", ondelete="RESTRICT"),
        CheckConstraint("(product_id IS NULL AND product_version IS NULL AND unknown_product_label IS NOT NULL) OR "
                        "(product_id IS NOT NULL AND product_version > 0 AND unknown_product_label IS NULL)",
                        name="ck_receiving_line_identity"),
        CheckConstraint("length(evidence_sha256)=64", name="ck_receiving_line_sha"),
        CheckConstraint("(quoted_price IS NULL AND delivered_price IS NULL AND "
                        "price_evidence_sha256 IS NULL) OR "
                        "(quoted_price IS NOT NULL AND delivered_price IS NOT NULL AND "
                        "length(price_evidence_sha256)=64)", name="ck_receiving_line_price_proof"),
    )


class ReceivingDiscrepancy(Base):
    __tablename__ = "receiving_discrepancies"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False)
    line_id: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_receiving_discrepancies_scope"),
        UniqueConstraint("organization_id", "store_id", "batch_id", "line_id", "kind",
                         name="uq_receiving_discrepancy_once"),
        ForeignKeyConstraint(["organization_id", "store_id", "batch_id"],
                             ["receiving_batches.organization_id", "receiving_batches.store_id",
                              "receiving_batches.id"], name="fk_receiving_discrepancy_batch", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "line_id"],
                             ["receiving_lines.organization_id", "receiving_lines.store_id",
                              "receiving_lines.id"], name="fk_receiving_discrepancy_line", ondelete="RESTRICT"),
        CheckConstraint("kind IN ('shortage','overage','wrong_item','price_change','identity_unknown')",
                        name="ck_receiving_discrepancy_kind"),
    )


class ReceivingDecision(Base):
    __tablename__ = "receiving_decisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False)
    discrepancy_id: Mapped[str] = mapped_column(String(36), nullable=False)
    batch_version: Mapped[int] = mapped_column(Integer(), nullable=False)
    reviewer_id: Mapped[str] = mapped_column(String(36), nullable=False)
    responsible_id: Mapped[str] = mapped_column(String(36), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "batch_id", "batch_version",
                         name="uq_receiving_decision_cas"),
        ForeignKeyConstraint(["organization_id", "store_id", "batch_id"],
                             ["receiving_batches.organization_id", "receiving_batches.store_id",
                              "receiving_batches.id"], name="fk_receiving_decision_batch", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "discrepancy_id"],
                             ["receiving_discrepancies.organization_id",
                              "receiving_discrepancies.store_id", "receiving_discrepancies.id"],
                             name="fk_receiving_decision_discrepancy", ondelete="RESTRICT"),
        CheckConstraint("decision IN ('accept_observation','request_recount',"
                        "'return_supplier_review','hold_identity','hold_price')",
                        name="ck_receiving_decision_kind"),
        CheckConstraint("batch_version>1 AND length(evidence_sha256)=64",
                        name="ck_receiving_decision_proof"),
    )


class ReceivingCommand(Base):
    __tablename__ = "receiving_commands"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict] = mapped_column(JSON(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "actor_id", "operation",
                         "idempotency_key", name="uq_receiving_commands_identity"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                             name="fk_receiving_commands_store", ondelete="RESTRICT"),
    )

# Make Base.metadata.create_all in isolated tests as fail-closed as Alembic.
@event.listens_for(ReceivingLine.__table__, "after_create")
@event.listens_for(ReceivingDiscrepancy.__table__, "after_create")
@event.listens_for(ReceivingDecision.__table__, "after_create")
def _immutable(_table, connection, **_kwargs):
    table = _table.name
    if connection.dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            connection.execute(DDL(
                f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                "BEGIN SELECT RAISE(ABORT,'receiving evidence is immutable'); END"))
    elif connection.dialect.name == "postgresql":
        connection.execute(DDL(
            "CREATE OR REPLACE FUNCTION deny_receiving_evidence_mutation() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'receiving evidence is immutable'; END; $$ LANGUAGE plpgsql"))
        connection.execute(DDL(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION deny_receiving_evidence_mutation()"))
