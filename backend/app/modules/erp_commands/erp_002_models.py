"""ERP-002: scoped internal ERP-command authority, NOT an ERP write adapter.

The four records are durable and additive: request -> human decision -> single
intent -> unknown/manual reconciliation. No table stores a guessed ERP success.
"""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import (Boolean, CheckConstraint, ForeignKeyConstraint, Index, Integer,
                        JSON, String, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime


class ERPCommandRequest(Base):
    __tablename__ = "erp_command_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(36), nullable=False)
    request_key: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False)
    parameters_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_erp_request_scope"),
        UniqueConstraint("organization_id", "store_id", "request_key", name="uq_erp_request_key"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                             name="fk_erp_request_store", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "requested_by"], ["subjects.organization_id", "subjects.id"],
                             name="fk_erp_request_person", ondelete="RESTRICT"),
        CheckConstraint("expected_version > 0", name="ck_erp_request_version"),
        CheckConstraint("length(target_digest)=64 AND length(evidence_digest)=64 "
                        "AND length(parameters_digest)=64 AND length(request_digest)=64",
                        name="ck_erp_request_digests"),
        CheckConstraint("action IN ('catalog_field_proposal','purchase_document_proposal',"
                        "'sales_return_proposal','sales_document_proposal','goods_receipt_document_proposal','catalog_maintenance_proposal','settlement_document_proposal')", name="ck_erp_request_action"),
    )


class ERPCommandApproval(Base):
    __tablename__ = "erp_command_approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    approved_by: Mapped[str] = mapped_column(String(36), nullable=False)
    decision: Mapped[str] = mapped_column(String(12), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_key: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_erp_approval_scope"),
        UniqueConstraint("organization_id", "store_id", "request_id", name="uq_erp_approval_once"),
        UniqueConstraint("organization_id", "store_id", "approval_key", name="uq_erp_approval_key"),
        ForeignKeyConstraint(["organization_id", "store_id", "request_id"],
                             ["erp_command_requests.organization_id", "erp_command_requests.store_id",
                              "erp_command_requests.id"], name="fk_erp_approval_request", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "approved_by"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_erp_approval_person", ondelete="RESTRICT"),
        CheckConstraint("decision IN ('approved','rejected')", name="ck_erp_approval_decision"),
        CheckConstraint("length(request_digest)=64", name="ck_erp_approval_digest"),
    )


class ERPCommandIntent(Base):
    __tablename__ = "erp_command_intents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    approval_id: Mapped[str] = mapped_column(String(36), nullable=False)
    executor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    target_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_erp_intent_scope"),
        UniqueConstraint("organization_id", "store_id", "request_id", name="uq_erp_intent_once"),
        UniqueConstraint("organization_id", "store_id", "approval_id", name="uq_erp_intent_approval_once"),
        UniqueConstraint("organization_id", "store_id", "idempotency_key", name="uq_erp_intent_key"),
        ForeignKeyConstraint(["organization_id", "store_id", "request_id"],
                             ["erp_command_requests.organization_id", "erp_command_requests.store_id",
                              "erp_command_requests.id"], name="fk_erp_intent_request", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "approval_id"],
                             ["erp_command_approvals.organization_id",
                              "erp_command_approvals.store_id", "erp_command_approvals.id"],
                             name="fk_erp_intent_approval", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "executor_id"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_erp_intent_executor", ondelete="RESTRICT"),
        CheckConstraint("length(request_digest)=64 AND length(target_digest)=64",
                        name="ck_erp_intent_digests"),
    )


class ERPCommandOutcome(Base):
    """Immutable terminal MANUAL reconciliation; never a network-send receipt."""
    __tablename__ = "erp_command_outcomes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    intent_id: Mapped[str] = mapped_column(String(36), nullable=False)
    verified_by: Mapped[str] = mapped_column(String(36), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    request_key: Mapped[str] = mapped_column(String(200), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "intent_id", name="uq_erp_outcome_once"),
        UniqueConstraint("organization_id", "store_id", "request_key", name="uq_erp_outcome_key"),
        ForeignKeyConstraint(["organization_id", "store_id", "intent_id"],
                             ["erp_command_intents.organization_id", "erp_command_intents.store_id",
                              "erp_command_intents.id"], name="fk_erp_outcome_intent", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "verified_by"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_erp_outcome_verifier", ondelete="RESTRICT"),
        CheckConstraint("outcome IN ('unknown','manual_rejected','verified_external')",
                        name="ck_erp_outcome_code"),
        CheckConstraint("length(evidence_sha256)=64", name="ck_erp_outcome_hash"),
    )
