"""ANA-005: immutable scoped analytical candidates and human decision receipts.

No official purchase order, on-hand ledger or ERP dispatch is represented here.
"""
from datetime import datetime

from sqlalchemy import (CheckConstraint, DDL, ForeignKeyConstraint, Index,
                        Integer, JSON, String, UniqueConstraint, event)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime
from backend.app.modules.catalog import models as catalog_models  # noqa: F401
from backend.app.modules.identity import models as identity_models  # noqa: F401


class PurchaseRecommendation(Base):
    __tablename__ = "purchase_recommendations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False)
    product_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    request_key: Mapped[str] = mapped_column(String(120), nullable=False)
    formula_version: Mapped[str] = mapped_column(String(60), nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_evidence: Mapped[dict] = mapped_column(JSON, nullable=False)
    projection: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id",
                         name="uq_purchase_recommendation_scope"),
        UniqueConstraint("organization_id", "store_id", "created_by", "request_key",
                         name="uq_purchase_recommendation_request"),
        ForeignKeyConstraint(["organization_id", "store_id"],
            ["stores.organization_id", "stores.id"],
            name="fk_purchase_recommendation_store", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "product_id"],
            ["catalog_products.organization_id", "catalog_products.store_id", "catalog_products.id"],
            name="fk_purchase_recommendation_product", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "created_by"],
            ["subjects.organization_id", "subjects.id"],
            name="fk_purchase_recommendation_creator", ondelete="RESTRICT"),
        CheckConstraint("product_version > 0", name="ck_purchase_recommendation_version"),
        CheckConstraint("length(source_digest)=64 AND length(candidate_digest)=64",
                        name="ck_purchase_recommendation_digests"),
        Index("ix_purchase_recommendation_scope_time", "organization_id", "store_id",
              "created_at", "id"),
    )


class PurchaseRecommendationDecision(Base):
    __tablename__ = "purchase_recommendation_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    recommendation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(36), nullable=False)
    decision_key: Mapped[str] = mapped_column(String(120), nullable=False)
    decision: Mapped[str] = mapped_column(String(12), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    expected_candidate_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity_pieces: Mapped[int | None] = mapped_column(Integer)
    local_draft: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "recommendation_id",
                         name="uq_purchase_recommendation_decision_once"),
        UniqueConstraint("organization_id", "store_id", "decided_by", "decision_key",
                         name="uq_purchase_recommendation_decision_key"),
        ForeignKeyConstraint(["organization_id", "store_id", "recommendation_id"],
            ["purchase_recommendations.organization_id", "purchase_recommendations.store_id",
             "purchase_recommendations.id"],
            name="fk_purchase_recommendation_decision_parent", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "decided_by"],
            ["subjects.organization_id", "subjects.id"],
            name="fk_purchase_recommendation_reviewer", ondelete="RESTRICT"),
        CheckConstraint("decision IN ('accept','modify','reject','defer')",
                        name="ck_purchase_recommendation_decision_choice"),
        CheckConstraint("length(expected_candidate_digest)=64",
                        name="ck_purchase_recommendation_decision_digest"),
        CheckConstraint("(decision IN ('accept','modify') AND quantity_pieces > 0 "
                        "AND local_draft IS NOT NULL) OR "
                        "(decision IN ('reject','defer') AND quantity_pieces IS NULL "
                        "AND local_draft IS NULL)",
                        name="ck_purchase_recommendation_decision_payload"),
    )


@event.listens_for(PurchaseRecommendation.__table__, "after_create")
@event.listens_for(PurchaseRecommendationDecision.__table__, "after_create")
def _immutable_records(table, connection, **_kwargs):
    if connection.dialect.name == "sqlite":
        for op in ("UPDATE", "DELETE"):
            connection.execute(DDL(
                f"CREATE TRIGGER {table.name}_no_{op.lower()} BEFORE {op} "
                f"ON {table.name} BEGIN "
                "SELECT RAISE(ABORT,'purchase recommendation evidence immutable'); END"))
    elif connection.dialect.name == "postgresql":
        connection.execute(DDL(
            "CREATE OR REPLACE FUNCTION deny_purchase_recommendation_mutation() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'purchase recommendation evidence immutable'; END; $$"))
        connection.execute(DDL(
            f"CREATE TRIGGER {table.name}_immutable BEFORE UPDATE OR DELETE "
            f"ON {table.name} FOR EACH ROW "
            "EXECUTE FUNCTION deny_purchase_recommendation_mutation()"))
