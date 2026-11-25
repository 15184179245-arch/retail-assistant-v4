"""ANA-002 immutable scoped analytic snapshots; never ledger inventory."""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import CheckConstraint, DDL, ForeignKeyConstraint, Index, Integer, JSON, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime
from backend.app.modules.stocktake import models as stocktake_models  # noqa: F401


class AnalyticsSnapshot(Base):
    __tablename__ = "analytics_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_session_version: Mapped[int] = mapped_column(Integer, nullable=False)
    metric_id: Mapped[str] = mapped_column(String(120), nullable=False)
    metric_version: Mapped[str] = mapped_column(String(40), nullable=False)
    formula_version: Mapped[str] = mapped_column(String(80), nullable=False)
    formula_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    result_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    coverage_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    confidence: Mapped[str] = mapped_column(String(30), nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    sources: Mapped[list] = mapped_column(JSON, nullable=False)
    source_last_recorded_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    snapshot_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_analytics_snapshots_scope"),
        ForeignKeyConstraint(["organization_id", "store_id", "session_id"],
            ["stocktake_sessions.organization_id", "stocktake_sessions.store_id",
             "stocktake_sessions.id"], name="fk_analytics_snapshot_stocktake", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "created_by"],
            ["subjects.organization_id", "subjects.id"], name="fk_analytics_snapshot_creator", ondelete="RESTRICT"),
        CheckConstraint("version > 0 AND source_session_version > 0", name="ck_analytics_snapshot_versions"),
        CheckConstraint("length(formula_digest) = 64 AND length(source_digest) = 64 "
            "AND length(result_digest) = 64", name="ck_analytics_snapshot_hashes"),
        CheckConstraint("coverage_kind IN ('partial_area','partial_shelf','unknown')",
            name="ck_analytics_snapshot_partial"),
        CheckConstraint("confidence IN ('partial_reviewed','partial_unreviewed',"
            "'unknown_coverage','insufficient_evidence')", name="ck_analytics_snapshot_confidence"),
        Index("ix_analytics_snapshot_scope_time", "organization_id", "store_id",
              "snapshot_at", "id"),
    )


@event.listens_for(AnalyticsSnapshot.__table__, "after_create")
def _protect_snapshots(_table, connection, **_kw):
    """Same protection for Base.metadata.create_all isolated tests."""
    if connection.dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            connection.execute(DDL(
                f"CREATE TRIGGER analytics_snapshot_block_{action.lower()} "
                f"BEFORE {action} ON analytics_snapshots BEGIN "
                "SELECT RAISE(ABORT,'analytics snapshot is immutable'); END"))
    elif connection.dialect.name == "postgresql":
        connection.execute(DDL(
            "CREATE OR REPLACE FUNCTION analytics_snapshot_immutable() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'analytics snapshot is immutable'; END; $$ LANGUAGE plpgsql"))
        connection.execute(DDL(
            "CREATE TRIGGER analytics_snapshot_guard BEFORE UPDATE OR DELETE "
            "ON analytics_snapshots FOR EACH ROW "
            "EXECUTE FUNCTION analytics_snapshot_immutable()"))
