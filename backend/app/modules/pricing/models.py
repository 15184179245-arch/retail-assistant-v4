"""Price source, rule-version, immutable quote and approval-binding models (SALE-001)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (Boolean, CheckConstraint, ForeignKeyConstraint, Index,
                        Integer, JSON, String, UniqueConstraint, event, text)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.db.types import UTCDateTime as _UTCDateTime


class UTCDateTime(_UTCDateTime):
    """Persist timezone-aware UTC values; normalize SQLite's naive results."""

    cache_ok = True


class PricingRule(Base):
    __tablename__ = "pricing_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    rule_key: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_precision: Mapped[int] = mapped_column(Integer, nullable=False)
    rounding: Mapped[str] = mapped_column(String(16), nullable=False)
    price_source_types: Mapped[list] = mapped_column(JSON, nullable=False)
    max_discount_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    approval_threshold_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    source_max_age_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    quote_ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    effective_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_pricing_rules_scope_id"),
        UniqueConstraint("organization_id", "store_id", "rule_key", "version",
                         name="uq_pricing_rules_key_version"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_pricing_rules_store_scope", ondelete="RESTRICT"),
        CheckConstraint("version > 0", name="ck_pricing_rules_version_positive"),
        CheckConstraint("length(currency) = 3 AND currency = upper(currency)",
                        name="ck_pricing_rules_currency"),
        CheckConstraint("amount_precision BETWEEN 0 AND 6", name="ck_pricing_rules_precision"),
        CheckConstraint("rounding IN ('ROUND_HALF_UP', 'ROUND_CEILING', 'ROUND_DOWN')",
                        name="ck_pricing_rules_rounding"),
        CheckConstraint("max_discount_bps BETWEEN 0 AND 10000 AND approval_threshold_bps BETWEEN 0 AND max_discount_bps",
                        name="ck_pricing_rules_discount_bounds"),
        CheckConstraint("source_max_age_seconds > 0 AND quote_ttl_seconds BETWEEN 60 AND 86400",
                        name="ck_pricing_rules_time_bounds"),
        CheckConstraint("effective_until IS NULL OR effective_until > effective_from",
                        name="ck_pricing_rules_window"),
        Index("ix_pricing_rules_active", "organization_id", "store_id", "rule_key", "active", "effective_from"),
    )


class PricingQuote(Base):
    __tablename__ = "pricing_quotes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(36), nullable=False)
    rule_key: Mapped[str] = mapped_column(String(120), nullable=False)
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    quote_key: Mapped[str] = mapped_column(String(200), nullable=False)
    requester_id: Mapped[str] = mapped_column(String(36), nullable=False)
    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[str] = mapped_column(String(200), nullable=False)
    object_version: Mapped[str] = mapped_column(String(100), nullable=False)
    product_id: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[str] = mapped_column(String(40), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_precision: Mapped[int] = mapped_column(Integer, nullable=False)
    rounding: Mapped[str] = mapped_column(String(16), nullable=False)
    list_price: Mapped[str] = mapped_column(String(40), nullable=False)
    unit_price: Mapped[str] = mapped_column(String(40), nullable=False)
    discount_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    final_amount: Mapped[str] = mapped_column(String(40), nullable=False)
    price_source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    price_source_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    price_source_version: Mapped[str] = mapped_column(String(120), nullable=False)
    price_source_snapshot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    price_source_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    source_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    cost_amount: Mapped[str | None] = mapped_column(String(40), nullable=True)
    cost_source_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    cost_source_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cost_observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_pricing_quotes_scope_id"),
        UniqueConstraint("organization_id", "store_id", "quote_key", name="uq_pricing_quotes_key"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_pricing_quotes_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "rule_id"],
                             ["pricing_rules.organization_id", "pricing_rules.store_id", "pricing_rules.id"],
                             name="fk_pricing_quotes_rule_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "requester_id"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_pricing_quotes_requester_scope", ondelete="RESTRICT"),
        CheckConstraint("status IN ('quoted', 'approval_required')", name="ck_pricing_quotes_status"),
        CheckConstraint("discount_bps BETWEEN 0 AND 10000", name="ck_pricing_quotes_discount"),
        CheckConstraint("length(currency) = 3 AND currency = upper(currency)", name="ck_pricing_quotes_currency"),
        CheckConstraint("amount_precision BETWEEN 0 AND 6", name="ck_pricing_quotes_precision"),
        CheckConstraint("rounding IN ('ROUND_HALF_UP', 'ROUND_CEILING', 'ROUND_DOWN')",
                        name="ck_pricing_quotes_rounding"),
        CheckConstraint("length(quote_key) > 0 AND length(object_type) > 0 AND length(object_id) > 0 "
                        "AND length(object_version) > 0 AND length(product_id) > 0",
                        name="ck_pricing_quotes_identity_nonempty"),
        CheckConstraint("length(snapshot_digest) = 64", name="ck_pricing_quotes_digest"),
        Index("ix_pricing_quotes_scope_time", "organization_id", "store_id", "created_at", "id"),
        Index("ix_pricing_quotes_requester", "organization_id", "store_id", "requester_id", "created_at"),
    )


class PricingQuoteApproval(Base):
    __tablename__ = "pricing_quote_approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    quote_id: Mapped[str] = mapped_column(String(36), nullable=False)
    approval_id: Mapped[str] = mapped_column(String(36), nullable=False)
    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[str] = mapped_column(String(200), nullable=False)
    object_version: Mapped[str] = mapped_column(String(100), nullable=False)
    snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(36), nullable=False)
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    bound_by: Mapped[str] = mapped_column(String(36), nullable=False)
    bound_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_validated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_pricing_quote_approvals_scope_id"),
        UniqueConstraint("organization_id", "store_id", "quote_id", name="uq_pricing_quote_approvals_quote"),
        UniqueConstraint("organization_id", "store_id", "approval_id", name="uq_pricing_quote_approvals_approval"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_pricing_quote_approvals_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "quote_id"],
                             ["pricing_quotes.organization_id", "pricing_quotes.store_id", "pricing_quotes.id"],
                             name="fk_pricing_quote_approvals_quote", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "approval_id"],
                             ["approval_requests.organization_id", "approval_requests.store_id", "approval_requests.id"],
                             name="fk_pricing_quote_approvals_approval", ondelete="RESTRICT"),
        CheckConstraint("state IN ('bound', 'revoked')", name="ck_pricing_quote_approvals_state"),
        CheckConstraint("length(snapshot_digest) = 64", name="ck_pricing_quote_approvals_digest"),
        CheckConstraint("expires_at > bound_at", name="ck_pricing_quote_approvals_window"),
        Index("ix_pricing_quote_approvals_state", "organization_id", "store_id", "state", "expires_at"),
    )


@event.listens_for(PricingQuote, "before_update")
def _reject_quote_update(mapper, connection, target):  # pragma: no cover - defensive
    raise ValueError("pricing quote snapshots are immutable")


@event.listens_for(PricingQuote, "before_delete")
def _reject_quote_delete(mapper, connection, target):  # pragma: no cover - defensive
    raise ValueError("pricing quote snapshots are immutable")


@event.listens_for(PricingRule, "before_update")
def _reject_rule_update(mapper, connection, target):  # pragma: no cover - defensive
    raise ValueError("pricing rules are append-only; create a new version")


@event.listens_for(PricingRule, "before_delete")
def _reject_rule_delete(mapper, connection, target):  # pragma: no cover - defensive
    raise ValueError("pricing rules are append-only; create a new version")


def _install_rule_guards(table, connection, **kw):
    dialect = connection.dialect.name
    if dialect == "sqlite":
        connection.exec_driver_sql(
            "CREATE TRIGGER IF NOT EXISTS pricing_rules_append_only_update "
            "BEFORE UPDATE ON pricing_rules BEGIN "
            "SELECT RAISE(ABORT, 'pricing rules are append-only'); END")
        connection.exec_driver_sql(
            "CREATE TRIGGER IF NOT EXISTS pricing_rules_append_only_delete "
            "BEFORE DELETE ON pricing_rules BEGIN "
            "SELECT RAISE(ABORT, 'pricing rules are append-only'); END")
    elif dialect == "postgresql":
        connection.exec_driver_sql(
            "CREATE OR REPLACE FUNCTION pricing_rules_append_only() RETURNS trigger "
            "AS $$ BEGIN RAISE EXCEPTION 'pricing rules are append-only'; END; "
            "$$ LANGUAGE plpgsql")
        connection.exec_driver_sql(
            "CREATE TRIGGER pricing_rules_append_only_guard BEFORE UPDATE OR DELETE "
            "ON pricing_rules FOR EACH ROW EXECUTE FUNCTION pricing_rules_append_only()")


event.listen(PricingRule.__table__, "after_create", _install_rule_guards)


class SourceCurrencyPolicyV2(Base):
    """Controlled append-only policy versions; there is no public creation API."""
    __tablename__ = "pricing_source_currency_policies_v2"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_system: Mapped[str] = mapped_column(String(40), nullable=False)
    policy_key: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    effective_until: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "source_system", "policy_key", "version", name="uq_currency_v2_version"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], ondelete="RESTRICT"),
        CheckConstraint("version > 0 AND state IN ('active', 'revoked')", name="ck_currency_v2_state"),
        CheckConstraint("effective_until > effective_from", name="ck_currency_v2_window"),
        CheckConstraint("length(currency) = 3 AND currency = upper(currency)", name="ck_currency_v2_currency"),
        CheckConstraint("length(evidence_ref) > 0 AND length(evidence_sha256) = 64", name="ck_currency_v2_evidence"),
    )


class PricingQuoteV2(Base):
    """Original server-derived quote and request receipt, separate from every v1 row."""
    __tablename__ = "pricing_quotes_v2"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    draft_id: Mapped[str] = mapped_column(String(36), nullable=False)
    requester_id: Mapped[str] = mapped_column(String(36), nullable=False)
    quote_key: Mapped[str] = mapped_column(String(200), nullable=False)
    line_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request: Mapped[dict] = mapped_column(JSON, nullable=False)
    authority: Mapped[dict] = mapped_column(JSON, nullable=False)
    context: Mapped[dict] = mapped_column(JSON, nullable=False)
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_quote_v2_scope"),
        UniqueConstraint("organization_id", "store_id", "quote_key", name="uq_quote_v2_key"),
        ForeignKeyConstraint(["organization_id", "store_id", "draft_id"], ["sales_drafts.organization_id", "sales_drafts.store_id", "sales_drafts.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "requester_id"], ["subjects.organization_id", "subjects.id"], ondelete="RESTRICT"),
        CheckConstraint("length(request_digest) = 64 AND length(snapshot_digest) = 64", name="ck_quote_v2_digests"),
    )


def _immutable_v2(mapper, connection, target):
    raise ValueError("v2 pricing facts are append-only")


for _v2_model in (SourceCurrencyPolicyV2, PricingQuoteV2):
    event.listen(_v2_model, "before_update", _immutable_v2)
    event.listen(_v2_model, "before_delete", _immutable_v2)
