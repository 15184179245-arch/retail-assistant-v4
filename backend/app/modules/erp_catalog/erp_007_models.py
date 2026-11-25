"""ERP-007 synthetic control plane and immutable per-action proof; no source updates."""
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


class ERPCatalogHead(Base):
    """Explicit synthetic projection; id equals the stable local product ID."""
    __tablename__='erp_catalog_maintenance_heads'
    id: Mapped[str]=mapped_column(String(36),primary_key=True)
    organization_id: Mapped[str]=mapped_column(String(36),nullable=False)
    store_id: Mapped[str]=mapped_column(String(36),nullable=False)
    version: Mapped[int]=mapped_column(Integer,nullable=False)
    remote_version: Mapped[int]=mapped_column(Integer,nullable=False)
    fields: Mapped[dict]=mapped_column(JSON,nullable=False)
    image_ref: Mapped[str | None]=mapped_column(String(200),nullable=True)
    busy_command_id: Mapped[str | None]=mapped_column(String(36),nullable=True)
    created_at: Mapped[datetime]=mapped_column(UTCDateTime(),nullable=False)
    updated_at: Mapped[datetime]=mapped_column(UTCDateTime(),nullable=False)
    __table_args__=(
        *_scope('ecmh'),
        _fk(['organization_id','store_id','id'],'catalog_products',['organization_id','store_id','id'],'fk_ecmh_product'),
        UniqueConstraint('organization_id','store_id','busy_command_id',name='uq_ecmh_busy'),
        CheckConstraint('version > 0 AND remote_version > 0',name='ck_ecmh_versions'),
        CheckConstraint('updated_at >= created_at',name='ck_ecmh_times'),
        CheckConstraint('busy_command_id IS NULL OR length(busy_command_id) BETWEEN 1 AND 36',name='ck_ecmh_busy'),
    )


class ERPCatalogCommand(Base):
    __tablename__='erp_catalog_maintenance_commands'
    id: Mapped[str]=mapped_column(String(36),primary_key=True)
    organization_id: Mapped[str]=mapped_column(String(36),nullable=False)
    store_id: Mapped[str]=mapped_column(String(36),nullable=False)
    product_id: Mapped[str]=mapped_column(String(36),nullable=False)
    request_id: Mapped[str]=mapped_column(String(36),nullable=False)
    operation: Mapped[str]=mapped_column(String(16),nullable=False)
    target_version: Mapped[int]=mapped_column(Integer,nullable=False)
    source_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    action_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    context_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    source_snapshot: Mapped[dict]=mapped_column(JSON,nullable=False)
    proposal_key: Mapped[str]=mapped_column(String(200),nullable=False)
    proposal_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    created_by: Mapped[str]=mapped_column(String(36),nullable=False)
    created_at: Mapped[datetime]=mapped_column(UTCDateTime(),nullable=False)
    __table_args__=(
        *_scope('ecmc'),
        *(UniqueConstraint('organization_id','store_id',c,name='uq_ecmc_'+c) for c in ('request_id','proposal_key')),
        _fk(['organization_id','store_id','product_id'],'erp_catalog_maintenance_heads',['organization_id','store_id','id'],'fk_ecmc_head'),
        _fk(['organization_id','store_id','request_id'],'erp_command_requests',['organization_id','store_id','id'],'fk_ecmc_request'),
        _fk(['organization_id','created_by'],'subjects',['organization_id','id'],'fk_ecmc_creator'),
        CheckConstraint("operation IN ('text_update','image_upload','image_bind')",name='ck_ecmc_operation'),
        CheckConstraint('target_version > 0',name='ck_ecmc_version'),
        CheckConstraint('length(source_digest)=64 AND length(action_digest)=64 AND length(context_digest)=64 AND length(proposal_digest)=64',name='ck_ecmc_digests'),
        CheckConstraint('length(proposal_key) BETWEEN 1 AND 200',name='ck_ecmc_key'),
        Index('ix_ecmc_created','organization_id','store_id','product_id','created_at','id'),
    )

class ERPCatalogAttempt(Base):
    __tablename__='erp_catalog_maintenance_attempts'
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
    proof: Mapped[dict | None]=mapped_column(JSON(none_as_null=True),nullable=True)
    result_remote_version: Mapped[int | None]=mapped_column(Integer,nullable=True)
    applied_at: Mapped[datetime | None]=mapped_column(UTCDateTime(),nullable=True)
    delivery_ref: Mapped[str | None]=mapped_column(String(200),nullable=True)
    external_ref: Mapped[str | None]=mapped_column(String(200),nullable=True)
    receipt_digest: Mapped[str | None]=mapped_column(String(64),nullable=True)
    created_at: Mapped[datetime]=mapped_column(UTCDateTime(),nullable=False)
    updated_at: Mapped[datetime]=mapped_column(UTCDateTime(),nullable=False)
    dispatched_at: Mapped[datetime | None]=mapped_column(UTCDateTime(),nullable=True)
    __table_args__=(
        *_scope('ecma'),
        *(UniqueConstraint('organization_id','store_id',c,name='uq_ecma_'+c) for c in ('command_id','intent_id','queue_key')),
        _fk(['organization_id','store_id','command_id'],'erp_catalog_maintenance_commands',['organization_id','store_id','id'],'fk_ecma_command'),
        _fk(['organization_id','store_id','intent_id'],'erp_command_intents',['organization_id','store_id','id'],'fk_ecma_intent'),
        _fk(['organization_id','executor_id'],'subjects',['organization_id','id'],'fk_ecma_executor'),
        _fk(['account_id'],'employee_accounts',['id'],'fk_ecma_account'),
        _fk(['session_id'],'auth_sessions',['id'],'fk_ecma_session'),
        CheckConstraint("state IN ('prepared','dispatched','unknown','sandbox_verified','failed','cancelled')",name='ck_ecma_state'),
        CheckConstraint("state != 'cancelled' OR (proof IS NULL AND result_remote_version IS NULL AND applied_at IS NULL AND delivery_ref IS NULL AND external_ref IS NULL AND receipt_digest IS NULL)", name='ck_ecma_cancelled'),
        CheckConstraint('version > 0 AND account_version > 0 AND authorization_epoch > 0',name='ck_ecma_versions'),
        CheckConstraint("(state IN ('prepared','cancelled') AND dispatched_at IS NULL) OR (state NOT IN ('prepared','cancelled') AND dispatched_at IS NOT NULL)",name='ck_ecma_dispatch'),
        CheckConstraint('applied_at IS NULL OR (dispatched_at IS NOT NULL AND applied_at >= dispatched_at AND updated_at >= applied_at)',name='ck_ecma_apply_time'),
        CheckConstraint('length(input_digest)=64 AND length(queue_digest)=64',name='ck_ecma_digests'),
        CheckConstraint('length(queue_key) BETWEEN 1 AND 200',name='ck_ecma_key'),
        CheckConstraint('receipt_digest IS NULL OR length(receipt_digest)=64',name='ck_ecma_receipt'),
        CheckConstraint("state NOT IN ('sandbox_verified','failed') OR (proof IS NOT NULL AND receipt_digest IS NOT NULL AND applied_at IS NOT NULL AND result_remote_version IS NOT NULL AND result_remote_version > 0)",name='ck_ecma_verified'),
        CheckConstraint("(state IN ('prepared','dispatched','unknown','cancelled') AND applied_at IS NULL) OR state IN ('sandbox_verified','failed')",name='ck_ecma_applied'),
        CheckConstraint('updated_at >= created_at AND authorization_expires_at > created_at AND (dispatched_at IS NULL OR dispatched_at >= created_at)',name='ck_ecma_times'),
        Index('ix_ecma_state','organization_id','store_id','state','created_at','id'),
    )


class ERPCatalogObservation(Base):
    __tablename__='erp_catalog_maintenance_observations'
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
        *_scope('ecmo'),
        UniqueConstraint('organization_id','store_id','attempt_id','observation_key',name='uq_ecmo_key'),
        _fk(['organization_id','store_id','attempt_id'],'erp_catalog_maintenance_attempts',['organization_id','store_id','id'],'fk_ecmo_attempt'),
        _fk(['organization_id','actor_id'],'subjects',['organization_id','id'],'fk_ecmo_actor'),
        CheckConstraint("kind IN ('dispatch_intent','ack','readback','network_error','cancelled')",name='ck_ecmo_kind'),
        CheckConstraint('length(evidence_digest)=64',name='ck_ecmo_digest'),
        CheckConstraint('length(observation_key) BETWEEN 1 AND 200',name='ck_ecmo_key'),
        Index('ix_ecmo_time','organization_id','store_id','attempt_id','created_at','id'),
    )


def _immutable(target,connection,**kw):
    table=target.name
    if connection.dialect.name=='sqlite':
        for op in ('update','delete'):
            connection.exec_driver_sql(f"CREATE TRIGGER trg_{table}_{op}_deny BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'erp catalog evidence is immutable'); END")
    elif connection.dialect.name=='postgresql':
        connection.exec_driver_sql(f"CREATE FUNCTION deny_{table}_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'erp catalog evidence is immutable'; END; $$")
        connection.exec_driver_sql(f'CREATE TRIGGER trg_{table}_deny BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION deny_{table}_mutation()')


for table in (ERPCatalogCommand.__table__,ERPCatalogObservation.__table__):
    event.listen(table,'after_create',_immutable)
