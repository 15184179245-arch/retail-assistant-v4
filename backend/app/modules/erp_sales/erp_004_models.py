"""ERP-004 command reservations, single dispatch intent and immutable facts.

Structure never authorizes. All values are explicit; synthetic receipts are
kept apart from formal ERP outcomes and SalesDraft.status/settlement.
"""
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, Integer, JSON, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.db.base import Base
from backend.app.modules.pricing.models import UTCDateTime


def _fk(columns,table,targets,name):
    return ForeignKeyConstraint(columns,[table+'.'+c for c in targets],name=name,ondelete='RESTRICT')


def _scope(prefix):
    return (UniqueConstraint('organization_id','store_id','id',name='uq_'+prefix+'_scope'),
        _fk(['organization_id','store_id'],'stores',['organization_id','id'],'fk_'+prefix+'_store'))


class ERPSalesCommand(Base):
    __tablename__='erp_sales_commands'
    id: Mapped[str]=mapped_column(String(36),primary_key=True)
    organization_id: Mapped[str]=mapped_column(String(36),nullable=False)
    store_id: Mapped[str]=mapped_column(String(36),nullable=False)
    draft_id: Mapped[str]=mapped_column(String(36),nullable=False)
    handoff_id: Mapped[str]=mapped_column(String(36),nullable=False)
    request_id: Mapped[str]=mapped_column(String(36),nullable=False)
    route: Mapped[str]=mapped_column(String(8),nullable=False)
    source_version: Mapped[int]=mapped_column(Integer,nullable=False)
    source_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    source_snapshot: Mapped[dict]=mapped_column(JSON,nullable=False)
    proposal_key: Mapped[str]=mapped_column(String(200),nullable=False)
    proposal_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    created_by: Mapped[str]=mapped_column(String(36),nullable=False)
    created_at: Mapped[datetime]=mapped_column(UTCDateTime(),nullable=False)
    __table_args__=(
        *_scope('ersc'),
        *(UniqueConstraint('organization_id','store_id',c,name='uq_ersc_'+c) for c in ('draft_id','handoff_id','request_id','proposal_key')),
        _fk(['organization_id','store_id','draft_id'],'sales_drafts',['organization_id','store_id','id'],'fk_ersc_draft'),
        _fk(['organization_id','store_id','handoff_id'],'sales_handoffs',['organization_id','store_id','id'],'fk_ersc_handoff'),
        _fk(['organization_id','store_id','request_id'],'erp_command_requests',['organization_id','store_id','id'],'fk_ersc_request'),
        _fk(['organization_id','created_by'],'subjects',['organization_id','id'],'fk_ersc_creator'),
        CheckConstraint("route IN ('api','pos')",name='ck_ersc_route'),
        CheckConstraint('source_version > 0',name='ck_ersc_version'),
        CheckConstraint('length(source_digest)=64 AND length(proposal_digest)=64',name='ck_ersc_digests'),
        CheckConstraint('length(proposal_key) BETWEEN 1 AND 200',name='ck_ersc_key'),
        Index('ix_ersc_created','organization_id','store_id','created_at','id'),
    )


class ERPSalesAttempt(Base):
    __tablename__='erp_sales_attempts'
    id: Mapped[str]=mapped_column(String(36),primary_key=True)
    organization_id: Mapped[str]=mapped_column(String(36),nullable=False)
    store_id: Mapped[str]=mapped_column(String(36),nullable=False)
    command_id: Mapped[str]=mapped_column(String(36),nullable=False)
    intent_id: Mapped[str]=mapped_column(String(36),nullable=False)
    queue_key: Mapped[str]=mapped_column(String(200),nullable=False)
    queue_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    input_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    executor_id: Mapped[str]=mapped_column(String(36),nullable=False)
    account_id: Mapped[str]=mapped_column(String(36),nullable=False)
    session_id: Mapped[str]=mapped_column(String(36),nullable=False)
    account_version: Mapped[int]=mapped_column(Integer,nullable=False)
    authorization_epoch: Mapped[int]=mapped_column(Integer,nullable=False)
    authorization_expires_at: Mapped[datetime]=mapped_column(UTCDateTime(),nullable=False)
    state: Mapped[str]=mapped_column(String(24),nullable=False)
    version: Mapped[int]=mapped_column(Integer,nullable=False)
    line_results: Mapped[list]=mapped_column(JSON,nullable=False)
    delivery_ref: Mapped[str | None]=mapped_column(String(200),nullable=True)
    external_ref: Mapped[str | None]=mapped_column(String(200),nullable=True)
    receipt_digest: Mapped[str | None]=mapped_column(String(64),nullable=True)
    created_at: Mapped[datetime]=mapped_column(UTCDateTime(),nullable=False)
    updated_at: Mapped[datetime]=mapped_column(UTCDateTime(),nullable=False)
    dispatched_at: Mapped[datetime | None]=mapped_column(UTCDateTime(),nullable=True)
    __table_args__=(
        *_scope('ersa'),
        *(UniqueConstraint('organization_id','store_id',c,name='uq_ersa_'+c) for c in ('command_id','intent_id','queue_key','external_ref')),
        _fk(['organization_id','store_id','command_id'],'erp_sales_commands',['organization_id','store_id','id'],'fk_ersa_command'),
        _fk(['organization_id','store_id','intent_id'],'erp_command_intents',['organization_id','store_id','id'],'fk_ersa_intent'),
        _fk(['organization_id','executor_id'],'subjects',['organization_id','id'],'fk_ersa_executor'),
        _fk(['account_id'],'employee_accounts',['id'],'fk_ersa_account'),
        _fk(['session_id'],'auth_sessions',['id'],'fk_ersa_session'),
        CheckConstraint("state IN ('prepared','dispatched','unknown','partial','sandbox_verified','failed')",name='ck_ersa_state'),
        CheckConstraint('version > 0 AND account_version > 0 AND authorization_epoch > 0',name='ck_ersa_versions'),
        CheckConstraint('length(input_digest)=64 AND length(queue_digest)=64',name='ck_ersa_digests'),
        CheckConstraint('length(queue_key) BETWEEN 1 AND 200',name='ck_ersa_key'),
        CheckConstraint('receipt_digest IS NULL OR length(receipt_digest)=64',name='ck_ersa_receipt'),
        CheckConstraint("state != 'sandbox_verified' OR (external_ref IS NOT NULL AND receipt_digest IS NOT NULL)",name='ck_ersa_verified'),
        CheckConstraint('updated_at >= created_at AND authorization_expires_at > created_at AND (dispatched_at IS NULL OR dispatched_at >= created_at)',name='ck_ersa_times'),
        Index('ix_ersa_state','organization_id','store_id','state','created_at','id'),
    )


class ERPSalesObservation(Base):
    __tablename__='erp_sales_observations'
    id: Mapped[str]=mapped_column(String(36),primary_key=True)
    organization_id: Mapped[str]=mapped_column(String(36),nullable=False)
    store_id: Mapped[str]=mapped_column(String(36),nullable=False)
    attempt_id: Mapped[str]=mapped_column(String(36),nullable=False)
    observation_key: Mapped[str]=mapped_column(String(200),nullable=False)
    kind: Mapped[str]=mapped_column(String(24),nullable=False)
    actor_id: Mapped[str]=mapped_column(String(36),nullable=False)
    evidence_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    evidence: Mapped[dict]=mapped_column(JSON,nullable=False)
    created_at: Mapped[datetime]=mapped_column(UTCDateTime(),nullable=False)
    __table_args__=(
        *_scope('erso'),
        UniqueConstraint('organization_id','store_id','attempt_id','observation_key',name='uq_erso_key'),
        _fk(['organization_id','store_id','attempt_id'],'erp_sales_attempts',['organization_id','store_id','id'],'fk_erso_attempt'),
        _fk(['organization_id','actor_id'],'subjects',['organization_id','id'],'fk_erso_actor'),
        CheckConstraint("kind IN ('dispatch_intent','ack','readback','network_error')",name='ck_erso_kind'),
        CheckConstraint('length(evidence_digest)=64',name='ck_erso_digest'),
        CheckConstraint('length(observation_key) BETWEEN 1 AND 200',name='ck_erso_key'),
        Index('ix_erso_time','organization_id','store_id','attempt_id','created_at','id'),
    )


def _immutable(target,connection,**kw):
    table=target.name
    if connection.dialect.name=='sqlite':
        for op in ('update','delete'):
            connection.exec_driver_sql(f"CREATE TRIGGER trg_{table}_{op}_deny BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'erp sales evidence is immutable'); END")
    elif connection.dialect.name=='postgresql':
        connection.exec_driver_sql(f"CREATE FUNCTION deny_{table}_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'erp sales evidence is immutable'; END; $$")
        connection.exec_driver_sql(f'CREATE TRIGGER trg_{table}_deny BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION deny_{table}_mutation()')


for table in (ERPSalesCommand.__table__,ERPSalesObservation.__table__):
    event.listen(table,'after_create',_immutable)
