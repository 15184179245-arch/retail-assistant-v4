"""Append-only stocktake evidence; no ledger inventory mutation."""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import (CheckConstraint, DDL, ForeignKeyConstraint, Index, Integer,
                        JSON, String, UniqueConstraint, event)
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime
from backend.app.modules.catalog import models as catalog_models  # noqa: F401
from backend.app.modules.locations import models as location_models  # noqa: F401
from backend.app.modules.media import models as media_models  # noqa: F401


class StocktakeSession(Base):
    __tablename__ = "stocktake_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    opened_by: Mapped[str] = mapped_column(String(36), nullable=False)
    coverage_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    coverage_location_id: Mapped[str | None] = mapped_column(String(36))
    coverage_note: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="open")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_stocktake_sessions_scope"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_stocktake_sessions_store", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "coverage_location_id"],
                             ["locations.organization_id", "locations.store_id", "locations.id"],
                             name="fk_stocktake_sessions_coverage", ondelete="RESTRICT"),
        CheckConstraint("coverage_kind IN ('partial_area','partial_shelf','unknown')",
                        name="ck_stocktake_sessions_coverage_kind"),
        CheckConstraint("status = 'open'", name="ck_stocktake_sessions_open"),
        CheckConstraint("version > 0", name="ck_stocktake_sessions_version"),
        CheckConstraint("length(coverage_note) BETWEEN 1 AND 500",
                        name="ck_stocktake_sessions_note"),
    )


class StocktakeObservation(Base):
    __tablename__ = "stocktake_observations"
    event_seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(36), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    supersedes_id: Mapped[str | None] = mapped_column(String(36))
    product_id: Mapped[str | None] = mapped_column(String(36))
    unknown_product_label: Mapped[str | None] = mapped_column(String(200))
    location_id: Mapped[str | None] = mapped_column(String(36))
    unknown_location_label: Mapped[str | None] = mapped_column(String(200))
    quantity: Mapped[str] = mapped_column(String(40), nullable=False)
    unit: Mapped[str] = mapped_column(String(24), nullable=False)
    media_id: Mapped[str | None] = mapped_column(String(36))
    observed_by: Mapped[str] = mapped_column(String(36), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_stocktake_observations_scope"),
        UniqueConstraint("organization_id", "store_id", "session_id", "id",
                         name="uq_stocktake_observations_session_id"),
        ForeignKeyConstraint(["organization_id", "store_id", "session_id"],
                             ["stocktake_sessions.organization_id",
                              "stocktake_sessions.store_id", "stocktake_sessions.id"],
                             name="fk_stocktake_observations_session", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "product_id"],
                             ["catalog_products.organization_id",
                              "catalog_products.store_id", "catalog_products.id"],
                             name="fk_stocktake_observations_product", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "location_id"],
                             ["locations.organization_id", "locations.store_id", "locations.id"],
                             name="fk_stocktake_observations_location", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "media_id"],
                             ["media_assets.organization_id", "media_assets.store_id",
                              "media_assets.id"],
                             name="fk_stocktake_observations_media", ondelete="RESTRICT"),
        UniqueConstraint("organization_id", "store_id", "session_id", "supersedes_id",
                         name="uq_stocktake_observations_single_correction"),
        ForeignKeyConstraint(["organization_id", "store_id", "session_id", "supersedes_id"],
                             ["stocktake_observations.organization_id", "stocktake_observations.store_id",
                              "stocktake_observations.session_id", "stocktake_observations.id"],
                             name="fk_stocktake_observations_prior", ondelete="RESTRICT"),
        CheckConstraint("kind IN ('observed','correction')", name="ck_stocktake_observations_kind"),
        CheckConstraint("(kind = 'observed' AND supersedes_id IS NULL) OR "
                        "(kind = 'correction' AND supersedes_id IS NOT NULL)",
                        name="ck_stocktake_observations_correction"),
        CheckConstraint("(product_id IS NOT NULL AND unknown_product_label IS NULL) OR "
                        "(product_id IS NULL AND unknown_product_label IS NOT NULL)",
                        name="ck_stocktake_observations_product_identity"),
        CheckConstraint("(location_id IS NOT NULL AND unknown_location_label IS NULL) OR "
                        "(location_id IS NULL AND unknown_location_label IS NOT NULL)",
                        name="ck_stocktake_observations_location_identity"),
        Index("ix_stocktake_observations_scope_seq", "organization_id", "store_id",
              "session_id", "event_seq"),
    )


@event.listens_for(StocktakeObservation.__table__, "after_create")
def _append_only(_table, connection, **_kwargs):
    if connection.dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            connection.execute(DDL(
                f"CREATE TRIGGER stocktake_observations_block_{action.lower()} "
                f"BEFORE {action} ON stocktake_observations BEGIN "
                "SELECT RAISE(ABORT,'stocktake observations append-only'); END"))
    elif connection.dialect.name == "postgresql":
        connection.execute(DDL(
            "CREATE OR REPLACE FUNCTION stocktake_observations_immutable() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'stocktake observations append-only'; END; $$ LANGUAGE plpgsql"))
        connection.execute(DDL(
            "CREATE TRIGGER stocktake_observations_immutable_guard "
            "BEFORE UPDATE OR DELETE ON stocktake_observations FOR EACH ROW "
            "EXECUTE FUNCTION stocktake_observations_immutable()"))


class StocktakeCommand(Base):
    __tablename__ = "stocktake_commands"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict] = mapped_column(JSON(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "actor_id", "operation",
                         "idempotency_key", name="uq_stocktake_commands_identity"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_stocktake_commands_store", ondelete="RESTRICT"),
    )
