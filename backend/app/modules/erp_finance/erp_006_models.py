"""ERP-006 settlement command reservations, single dispatch intent and immutable facts.

Structure never authorizes. Synthetic receipts never post official ledger entries
or automatically move bank funds.
"""
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, Integer, JSON, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.db.base import Base
from backend.app.modules.pricing.models import UTCDateTime


def _fk(columns, table, targets, name):
    return ForeignKeyConstraint(columns, [table + '.' + c for c in targets], name=name, ondelete='RESTRICT')


def _scope(prefix):
    return (
        UniqueConstraint('organization_id', 'store_id', 'id', name='uq_' + prefix + '_scope'),
        _fk(['organization_id', 'store_id'], 'stores', ['organization_id', 'id'], 'fk_' + prefix + '_store'),
    )


class ERPSettlementCommand(Base):
    __tablename__ = 'erp_settlement_commands'

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workpaper_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    route: Mapped[str] = mapped_column(String(8), nullable=False)
    operation: Mapped[str] = mapped_column(String(24), nullable=False)
    party_type: Mapped[str] = mapped_column(String(16), nullable=False)
    party_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    settlement_amount: Mapped[str] = mapped_column(String(32), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    proposal_key: Mapped[str] = mapped_column(String(200), nullable=False)
    proposal_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        *_scope('ersc_fin'),
        UniqueConstraint('organization_id', 'store_id', 'workpaper_id', name='uq_ersc_fin_workpaper'),
        UniqueConstraint('organization_id', 'store_id', 'request_id', name='uq_ersc_fin_request'),
        UniqueConstraint('organization_id', 'store_id', 'proposal_key', name='uq_ersc_fin_proposal_key'),
        _fk(['organization_id', 'store_id', 'workpaper_id'], 'finance_workpapers', ['organization_id', 'store_id', 'id'], 'fk_ersc_fin_workpaper'),
        _fk(['organization_id', 'store_id', 'request_id'], 'erp_command_requests', ['organization_id', 'store_id', 'id'], 'fk_ersc_fin_request'),
        _fk(['organization_id', 'created_by'], 'subjects', ['organization_id', 'id'], 'fk_ersc_fin_creator'),
        CheckConstraint("route = 'api'", name='ck_ersc_fin_route'),
        CheckConstraint("operation IN ('receipt_allocation', 'payment_allocation')", name='ck_ersc_fin_op'),
        CheckConstraint("party_type IN ('customer', 'supplier')", name='ck_ersc_fin_party'),
        CheckConstraint("currency IN ('CNY', 'USD')", name='ck_ersc_fin_curr'),
        CheckConstraint('source_version > 0', name='ck_ersc_fin_ver'),
        CheckConstraint('length(source_digest)=64 AND length(proposal_digest)=64', name='ck_ersc_fin_digests'),
        CheckConstraint('length(proposal_key) BETWEEN 1 AND 200', name='ck_ersc_fin_key'),
        Index('ix_ersc_fin_created', 'organization_id', 'store_id', 'created_at', 'id'),
    )


class ERPSettlementAttempt(Base):
    __tablename__ = 'erp_settlement_attempts'

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    command_id: Mapped[str] = mapped_column(String(36), nullable=False)
    intent_id: Mapped[str] = mapped_column(String(36), nullable=False)
    queue_key: Mapped[str] = mapped_column(String(200), nullable=False)
    queue_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    executor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_version: Mapped[int] = mapped_column(Integer, nullable=False)
    authorization_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    authorization_expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    allocation_results: Mapped[list] = mapped_column(JSON, nullable=False)
    delivery_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    receipt_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    dispatched_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        *_scope('ersa_fin'),
        UniqueConstraint('organization_id', 'store_id', 'command_id', name='uq_ersa_fin_command'),
        UniqueConstraint('organization_id', 'store_id', 'intent_id', name='uq_ersa_fin_intent'),
        UniqueConstraint('organization_id', 'store_id', 'queue_key', name='uq_ersa_fin_queue_key'),
        _fk(['organization_id', 'store_id', 'command_id'], 'erp_settlement_commands', ['organization_id', 'store_id', 'id'], 'fk_ersa_fin_cmd'),
        _fk(['organization_id', 'store_id', 'intent_id'], 'erp_command_intents', ['organization_id', 'store_id', 'id'], 'fk_ersa_fin_intent'),
        _fk(['organization_id', 'executor_id'], 'subjects', ['organization_id', 'id'], 'fk_ersa_fin_exec'),
        ForeignKeyConstraint(['account_id'], ['employee_accounts.id'], name='fk_ersa_fin_acc', ondelete="RESTRICT"),
        ForeignKeyConstraint(['session_id'], ['auth_sessions.id'], name='fk_ersa_fin_sess', ondelete="RESTRICT"),
        CheckConstraint("state IN ('staged', 'dispatched', 'accepted', 'rejected', 'unknown')", name='ck_ersa_fin_state'),
        CheckConstraint('version > 0', name='ck_ersa_fin_ver'),
        CheckConstraint('account_version > 0', name='ck_ersa_fin_acc_ver'),
        CheckConstraint('authorization_epoch > 0', name='ck_ersa_fin_epoch'),
        CheckConstraint('length(queue_digest)=64 AND length(input_digest)=64', name='ck_ersa_fin_digests'),
        CheckConstraint('length(queue_key) BETWEEN 1 AND 200', name='ck_ersa_fin_key'),
        Index('ix_ersa_fin_state', 'organization_id', 'store_id', 'state', 'updated_at'),
    )


class ERPSettlementObservation(Base):
    __tablename__ = 'erp_settlement_observations'

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(36), nullable=False)
    command_id: Mapped[str] = mapped_column(String(36), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    observed_state: Mapped[str] = mapped_column(String(24), nullable=False)
    receipt_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, nullable=False)
    recorded_by: Mapped[str] = mapped_column(String(36), nullable=False)

    __table_args__ = (
        *_scope('erso_fin'),
        _fk(['organization_id', 'store_id', 'attempt_id'], 'erp_settlement_attempts', ['organization_id', 'store_id', 'id'], 'fk_erso_fin_att'),
        _fk(['organization_id', 'store_id', 'command_id'], 'erp_settlement_commands', ['organization_id', 'store_id', 'id'], 'fk_erso_fin_cmd'),
        _fk(['organization_id', 'recorded_by'], 'subjects', ['organization_id', 'id'], 'fk_erso_fin_rec'),
        CheckConstraint("observed_state IN ('dispatched', 'accepted', 'rejected', 'unknown')", name='ck_erso_fin_state'),
        CheckConstraint('length(receipt_digest)=64', name='ck_erso_fin_digest'),
        Index('ix_erso_fin_observed', 'organization_id', 'store_id', 'attempt_id', 'observed_at'),
    )


def _immutable(target, connection, **kw):
    table = target.name
    if connection.dialect.name == 'sqlite':
        for op in ('update', 'delete'):
            connection.exec_driver_sql(f"CREATE TRIGGER trg_{table}_{op}_deny BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'erp settlement evidence is immutable'); END")
    elif connection.dialect.name == 'postgresql':
        connection.exec_driver_sql(f"CREATE FUNCTION deny_{table}_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'erp settlement evidence is immutable'; END; $$")
        connection.exec_driver_sql(f'CREATE TRIGGER trg_{table}_deny BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION deny_{table}_mutation()')


for table in (ERPSettlementCommand.__table__, ERPSettlementObservation.__table__):
    event.listen(table, 'after_create', _immutable)
