"""ERP-003 synthetic dispatch records, separate from formal ERP outcomes.

No model here permits a production-verification claim or stores credentials.
The executor identity is an authorization snapshot, never an authorization grant.
"""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime


class ERPSandboxBatch(Base):
    __tablename__ = "erp_sandbox_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    queue_key: Mapped[str] = mapped_column(String(200), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    adapter_id: Mapped[str] = mapped_column(String(40), nullable=False, default="synthetic.erp.v1")
    contract_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="synthetic")
    executor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_version: Mapped[int] = mapped_column(Integer, nullable=False)
    authorization_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    authorization_expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="prepared")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    dispatched_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_erp_sandbox_batch_scope"),
        UniqueConstraint("organization_id", "store_id", "queue_key", name="uq_erp_sandbox_queue_key"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"],
                             name="fk_erp_sandbox_batch_store", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "executor_id"], ["subjects.organization_id", "subjects.id"],
                             name="fk_erp_sandbox_batch_executor", ondelete="RESTRICT"),
        CheckConstraint("mode = 'synthetic' AND adapter_id = 'synthetic.erp.v1' AND contract_version = 1",
                        name="ck_erp_sandbox_contract"),
        CheckConstraint("state IN ('prepared','dispatched','unknown','partial','sandbox_verified','failed')",
                        name="ck_erp_sandbox_batch_state"),
        CheckConstraint("version > 0 AND account_version > 0 AND authorization_epoch > 0",
                        name="ck_erp_sandbox_batch_versions"),
        CheckConstraint("length(input_digest) = 64 AND length(queue_key) BETWEEN 1 AND 200",
                        name="ck_erp_sandbox_batch_input"),
        CheckConstraint("updated_at >= created_at AND authorization_expires_at > created_at",
                        name="ck_erp_sandbox_batch_times"),
        CheckConstraint("state != 'prepared' OR dispatched_at IS NULL", name="ck_erp_sandbox_not_sent"),
        CheckConstraint("state != 'dispatched' OR dispatched_at IS NOT NULL", name="ck_erp_sandbox_sent"),
        Index("ix_erp_sandbox_batch_state", "organization_id", "store_id", "state", "created_at"),
    )


class ERPSandboxLine(Base):
    __tablename__ = "erp_sandbox_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False)
    intent_id: Mapped[str] = mapped_column(String(36), nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    target_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_parameters: Mapped[dict] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    external_ref: Mapped[str | None] = mapped_column(String(200))
    receipt_digest: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_erp_sandbox_line_scope"),
        UniqueConstraint("organization_id", "store_id", "batch_id", "line_no", name="uq_erp_sandbox_line_no"),
        UniqueConstraint("organization_id", "store_id", "intent_id", name="uq_erp_sandbox_intent_once"),
        ForeignKeyConstraint(["organization_id", "store_id", "batch_id"],
                             ["erp_sandbox_batches.organization_id", "erp_sandbox_batches.store_id", "erp_sandbox_batches.id"],
                             name="fk_erp_sandbox_line_batch", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "intent_id"],
                             ["erp_command_intents.organization_id", "erp_command_intents.store_id", "erp_command_intents.id"],
                             name="fk_erp_sandbox_line_intent", ondelete="RESTRICT"),
        CheckConstraint("line_no BETWEEN 1 AND 50 AND version > 0", name="ck_erp_sandbox_line_versions"),
        CheckConstraint("state IN ('pending','unknown','sandbox_verified','failed')", name="ck_erp_sandbox_line_state"),
        CheckConstraint("length(request_digest)=64 AND length(target_digest)=64 AND length(parameters_digest)=64 AND length(input_digest)=64",
                        name="ck_erp_sandbox_line_digests"),
        CheckConstraint("receipt_digest IS NULL OR length(receipt_digest)=64", name="ck_erp_sandbox_line_receipt"),
        CheckConstraint("state != 'sandbox_verified' OR (external_ref IS NOT NULL AND length(external_ref)>0 AND receipt_digest IS NOT NULL)",
                        name="ck_erp_sandbox_line_verified"),
    )


class ERPSandboxObservation(Base):
    __tablename__ = "erp_sandbox_observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False)
    observation_key: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    http_status: Mapped[int | None] = mapped_column(Integer)
    normalized_receipt: Mapped[dict | None] = mapped_column(JSON)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "batch_id", "observation_key", name="uq_erp_sandbox_observation"),
        ForeignKeyConstraint(["organization_id", "store_id", "batch_id"],
                             ["erp_sandbox_batches.organization_id", "erp_sandbox_batches.store_id", "erp_sandbox_batches.id"],
                             name="fk_erp_sandbox_observation_batch", ondelete="RESTRICT"),
        CheckConstraint("kind IN ('send_intent','send_error','readback_receipt','readback_error')", name="ck_erp_sandbox_observation_kind"),
        CheckConstraint("length(evidence_digest)=64 AND length(observation_key) BETWEEN 1 AND 200", name="ck_erp_sandbox_observation_input"),
        CheckConstraint("http_status IS NULL OR http_status BETWEEN 100 AND 599", name="ck_erp_sandbox_observation_http"),
    )
