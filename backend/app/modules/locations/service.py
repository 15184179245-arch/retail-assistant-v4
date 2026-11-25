from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.audit.service import record_event
from backend.app.modules.catalog import repository as catalog
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.locations.models import Location, PlacementAssertion

READ = "locations.read"
WRITE = "locations.write"
KINDS = ("area", "temporary", "unknown", "rack", "shelf", "bin")


class LocationError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise LocationError("location_time_invalid")
    return current.astimezone(timezone.utc)


def _require(session: Session, actor: AuthenticatedSubject, permission: str, now: datetime) -> None:
    try:
        authorize_scope(session, actor, organization_id=actor.organization_id,
                        store_id=actor.store_id, permission=permission, now=now)
    except HTTPException as error:
        raise LocationError("location_permission_denied") from error


def _location(session: Session, actor: AuthenticatedSubject, location_id: str) -> Location:
    location = session.scalar(select(Location).where(
        Location.organization_id == actor.organization_id,
        Location.store_id == actor.store_id,
        Location.id == location_id,
    ))
    if location is None:
        raise LocationError("location_not_found")
    return location


def _check_parent(session: Session, actor: AuthenticatedSubject, parent_id: str | None,
                  now: datetime) -> Location | None:
    if parent_id is None:
        return None
    parent = _location(session, actor, parent_id)
    seen = {parent.id}
    cursor = parent
    while cursor.parent_id:
        cursor = _location(session, actor, cursor.parent_id)
        if cursor.id in seen:
            raise LocationError("location_cycle")
        seen.add(cursor.id)
    return parent


def create_location(session: Session, actor: AuthenticatedSubject, *, name: str, kind: str,
                    parent_id: str | None = None, now: datetime | None = None) -> Location:
    current = _now(now)
    _require(session, actor, WRITE, current)
    if kind not in KINDS or not 1 <= len(name) <= 120:
        raise LocationError("location_invalid")
    _check_parent(session, actor, parent_id, current)
    location = Location(id=str(uuid4()), organization_id=actor.organization_id,
                        store_id=actor.store_id, parent_id=parent_id, kind=kind,
                        name=name, active=True, created_at=current, updated_at=current)
    session.add(location)
    session.flush()
    record_event(session, actor_id=actor.subject_id, organization_id=actor.organization_id,
                 store_id=actor.store_id, action="locations.created", target_type="location",
                 target_id=location.id, idempotency_key=f"location:{location.id}",
                 details={"kind": kind, "parent_id": parent_id or "root"}, occurred_at=current)
    return location


def assert_placement(session: Session, actor: AuthenticatedSubject, *, location_id: str,
                     product_id: str, kind: str, idempotency_key: str,
                     now: datetime | None = None) -> PlacementAssertion:
    current = _now(now)
    _require(session, actor, WRITE, current)
    if kind not in ("present", "ended") or not 1 <= len(idempotency_key) <= 200:
        raise LocationError("location_assertion_invalid")
    _location(session, actor, location_id)
    product = catalog.find_product(session, actor.organization_id, actor.store_id, product_id)
    if product is None:
        raise LocationError("location_product_not_found")
    existing = session.scalar(select(PlacementAssertion).where(
        PlacementAssertion.organization_id == actor.organization_id,
        PlacementAssertion.store_id == actor.store_id,
        PlacementAssertion.idempotency_key == idempotency_key,
    ))
    if existing is not None:
        if existing.location_id != location_id or existing.product_id != product_id or existing.kind != kind:
            raise LocationError("location_assertion_conflict")
        return existing
    if kind == "ended":
        latest = session.scalar(select(PlacementAssertion).where(
            PlacementAssertion.organization_id == actor.organization_id,
            PlacementAssertion.store_id == actor.store_id,
            PlacementAssertion.product_id == product_id,
            PlacementAssertion.location_id == location_id,
        ).order_by(PlacementAssertion.event_seq.desc()).limit(1))
        if latest is None or latest.kind != "present":
            raise LocationError("location_not_present")
    assertion = PlacementAssertion(
        id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        product_id=product_id, location_id=location_id, kind=kind,
        idempotency_key=idempotency_key, asserted_by=actor.subject_id, asserted_at=current)
    session.add(assertion)
    session.flush()
    record_event(session, actor_id=actor.subject_id, organization_id=actor.organization_id,
                 store_id=actor.store_id, action=f"locations.placement_{kind}",
                 target_type="placement_assertion", target_id=assertion.id,
                 idempotency_key=f"placement:{idempotency_key}",
                 details={"product_id": product_id, "location_id": location_id},
                 occurred_at=current)
    return assertion


def placement_history(session: Session, actor: AuthenticatedSubject, *,
                      location_id: str | None = None, product_id: str | None = None,
                      limit: int = 100, now: datetime | None = None) -> list[PlacementAssertion]:
    current = _now(now)
    _require(session, actor, READ, current)
    query = select(PlacementAssertion).where(
        PlacementAssertion.organization_id == actor.organization_id,
        PlacementAssertion.store_id == actor.store_id,
    )
    if location_id is not None:
        _location(session, actor, location_id)
        query = query.where(PlacementAssertion.location_id == location_id)
    if product_id is not None:
        query = query.where(PlacementAssertion.product_id == product_id)
    return list(session.scalars(query.order_by(PlacementAssertion.asserted_at, PlacementAssertion.id).limit(limit)))
