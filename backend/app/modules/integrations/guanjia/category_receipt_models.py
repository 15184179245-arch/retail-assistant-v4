"""Candidate category facts; their existence never grants disclosure permission.

Native IDs and subjects have existence/scope FKs only. The later Host must
revalidate exact native Run, actor, tool, input, output and current authority.
"""
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime


class GuanjiaCategoryReceipt(Base):
    __tablename__ = 'guanjia_category_receipts'

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    requester_id: Mapped[str] = mapped_column(String(36), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    step_id: Mapped[str] = mapped_column(String(36), nullable=False)
    step_kind: Mapped[str] = mapped_column(String(16), nullable=False, default='tool')
    pi_receipt_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation: Mapped[str] = mapped_column(String(24), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    directory_artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    directory_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    directory_observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint('organization_id', 'store_id', 'run_id', 'step_id', name='uq_gj_category_step'),
        UniqueConstraint('pi_receipt_id', name='uq_gj_category_native_receipt'),
        ForeignKeyConstraint(['organization_id', 'store_id', 'run_id', 'step_id', 'step_kind'],
            ['agent_steps.organization_id', 'agent_steps.store_id', 'agent_steps.run_id', 'agent_steps.id', 'agent_steps.kind'],
            name='fk_gj_category_step', ondelete='RESTRICT'),
        ForeignKeyConstraint(['pi_receipt_id'], ['agent_pi_tool_receipts.id'],
            name='fk_gj_category_native', ondelete='RESTRICT'),
        ForeignKeyConstraint(['organization_id', 'requester_id'], ['subjects.organization_id', 'subjects.id'],
            name='fk_gj_category_requester', ondelete='RESTRICT'),
        ForeignKeyConstraint(['organization_id', 'agent_id'], ['subjects.organization_id', 'subjects.id'],
            name='fk_gj_category_agent', ondelete='RESTRICT'),
        CheckConstraint("step_kind = 'tool'", name='ck_gj_category_kind'),
        CheckConstraint("operation IN ('category_discovery', 'category_products')", name='ck_gj_category_operation'),
        CheckConstraint('length(input_sha256) = 64 AND length(directory_artifact_sha256) = 64 '
            'AND length(directory_content_sha256) = 64 AND length(result_sha256) = 64', name='ck_gj_category_digest'),
    )
