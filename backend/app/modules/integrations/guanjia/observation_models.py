"""Committed source observations, never a capability or replay grant."""
from datetime import datetime
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime


class GuanjiaObservationReceipt(Base):
    __tablename__ = 'guanjia_observation_receipts'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    requester_id: Mapped[str] = mapped_column(String(36), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    step_id: Mapped[str] = mapped_column(String(36), nullable=False)
    step_kind: Mapped[str] = mapped_column(String(16), nullable=False, default='tool')
    pi_receipt_id: Mapped[str] = mapped_column(String(36), nullable=False)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False)
    product_version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(24), nullable=False)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint('organization_id','store_id','run_id','step_id',name='uq_gj_observation_step'),
        UniqueConstraint('pi_receipt_id',name='uq_gj_observation_native_receipt'),
        ForeignKeyConstraint(['organization_id','store_id','run_id','step_id','step_kind'],
            ['agent_steps.organization_id','agent_steps.store_id','agent_steps.run_id','agent_steps.id','agent_steps.kind'],
            name='fk_gj_observation_step',ondelete='RESTRICT'),
        ForeignKeyConstraint(['pi_receipt_id'],['agent_pi_tool_receipts.id'],name='fk_gj_observation_native',ondelete='RESTRICT'),
        ForeignKeyConstraint(['organization_id','store_id','product_id'],
            ['catalog_products.organization_id','catalog_products.store_id','catalog_products.id'],name='fk_gj_observation_product',ondelete='RESTRICT'),
        ForeignKeyConstraint(['organization_id','store_id','snapshot_id'],
            ['catalog_snapshots.organization_id','catalog_snapshots.store_id','catalog_snapshots.id'],name='fk_gj_observation_snapshot',ondelete='RESTRICT'),
        ForeignKeyConstraint(['organization_id','requester_id'],['subjects.organization_id','subjects.id'],name='fk_gj_observation_requester',ondelete='RESTRICT'),
        ForeignKeyConstraint(['organization_id','agent_id'],['subjects.organization_id','subjects.id'],name='fk_gj_observation_agent',ondelete='RESTRICT'),
        CheckConstraint("step_kind = 'tool' AND product_version > 0",name='ck_gj_observation_identity'),
        CheckConstraint("operation IN ('live_prices','live_stock')",name='ck_gj_observation_operation'),
        CheckConstraint('length(result_sha256) = 64 AND length(snapshot_sha256) = 64 AND length(snapshot_content_sha256) = 64',name='ck_gj_observation_digest'),
    )
