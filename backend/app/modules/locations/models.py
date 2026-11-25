from datetime import datetime

from sqlalchemy import (CheckConstraint, DDL, ForeignKeyConstraint, Index, Integer,
                        String, UniqueConstraint, event)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime


class Location(Base):
    __tablename__ = "locations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_locations_scope_id"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_locations_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "parent_id"],
                             ["locations.organization_id", "locations.store_id", "locations.id"],
                             name="fk_locations_parent_scope", ondelete="RESTRICT"),
        CheckConstraint("kind IN ('area','temporary','unknown','rack','shelf','bin')",
                        name="ck_locations_kind"),
        Index("ix_locations_scope_parent", "organization_id", "store_id", "parent_id"),
    )


class PlacementAssertion(Base):
    """Append-only presence/end assertions; history is never overwritten."""

    __tablename__ = "placement_assertions"

    event_seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(36), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False)
    location_id: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    asserted_by: Mapped[str] = mapped_column(String(36), nullable=False)
    asserted_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_placement_assertions_scope_id"),
        UniqueConstraint("organization_id", "store_id", "idempotency_key",
                         name="uq_placement_assertions_idem"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_placement_assertions_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "product_id"],
                             ["catalog_products.organization_id", "catalog_products.store_id",
                              "catalog_products.id"],
                             name="fk_placement_assertions_product_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "location_id"],
                             ["locations.organization_id", "locations.store_id", "locations.id"],
                             name="fk_placement_assertions_location_scope", ondelete="RESTRICT"),
        CheckConstraint("kind IN ('present','ended')", name="ck_placement_assertions_kind"),
        Index("ix_placement_assertions_product", "organization_id", "store_id", "product_id"),
    )


@event.listens_for(PlacementAssertion.__table__, "after_create")
def _install_append_only_trigger(target, connection, **_kwargs) -> None:
    """Keep ORM-created schemas (tests/local) as immutable as migrations."""
    if connection.dialect.name == "sqlite":
        connection.execute(DDL(
            "CREATE TRIGGER IF NOT EXISTS placement_assertions_append_only_update "
            "BEFORE UPDATE ON placement_assertions "
            "BEGIN SELECT RAISE(ABORT, 'placement assertions are append-only'); END"))
        connection.execute(DDL(
            "CREATE TRIGGER IF NOT EXISTS placement_assertions_append_only_delete "
            "BEFORE DELETE ON placement_assertions "
            "BEGIN SELECT RAISE(ABORT, 'placement assertions are append-only'); END"))
    elif connection.dialect.name == "postgresql":
        connection.execute(DDL(
            "CREATE OR REPLACE FUNCTION placement_assertions_append_only() RETURNS trigger "
            "AS $$ BEGIN RAISE EXCEPTION 'placement assertions are append-only'; END; "
            "$$ LANGUAGE plpgsql"))
        connection.execute(DDL(
            "CREATE TRIGGER placement_assertions_append_only_guard "
            "BEFORE UPDATE OR DELETE ON placement_assertions FOR EACH ROW "
            "EXECUTE FUNCTION placement_assertions_append_only()"))
