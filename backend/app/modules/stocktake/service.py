"""STK-003 append-only partial count observations, never ledger writes."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.audit.service import record_event
from backend.app.modules.catalog.models import CatalogProduct
from backend.app.modules.identity.models import Subject
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.knowledge.commands import execute_command
from backend.app.modules.locations.models import Location
from backend.app.modules.media.models import MediaAsset
from backend.app.modules.stocktake.models import StocktakeSession, StocktakeObservation, StocktakeCommand
from backend.app.modules.stocktake.schemas import (SessionCreate, ObservationCreate,
                                                   StocktakeSessionOut, ObservationOut)


def _now():
    return datetime.now(timezone.utc)


def _scope(model, actor):
    return (model.organization_id == actor.organization_id, model.store_id == actor.store_id)


def _authorize(session, actor, permission):
    authorize_scope(session, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission=permission)
    if session.scalar(select(Subject.subject_type).where(
            Subject.organization_id == actor.organization_id,
            Subject.id == actor.subject_id)) != "person":
        raise HTTPException(403, "stocktake_person_required")


def _session(session, actor, sid):
    row = session.scalar(select(StocktakeSession).where(
        *_scope(StocktakeSession, actor), StocktakeSession.id == sid))
    if row is None:
        raise HTTPException(404, "stocktake_session_not_found")
    return row


def _event(session, actor, sid, event_id):
    row = session.scalar(select(StocktakeObservation).where(
        *_scope(StocktakeObservation, actor), StocktakeObservation.session_id == sid,
        StocktakeObservation.id == event_id))
    if row is None:
        raise HTTPException(404, "stocktake_observation_not_found")
    return row


def _cmd(session, actor, operation, key, payload, handler):
    connection = session.connection()
    if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")
    with session.begin_nested():
        try:
            return execute_command(session, actor, operation=operation,
                                   key=key, payload=payload, execute=handler,
                                   receipt_model=StocktakeCommand)
        except HTTPException as error:
            if error.detail in ("idempotency_conflict", "knowledge_command_incomplete",
                                "knowledge_command_conflict"):
                raise HTTPException(409, "stocktake_idempotency_conflict") from None
            raise


def _audit(session, actor, action, object_type, object_id, details):
    record_event(session, actor_id=actor.subject_id, organization_id=actor.organization_id,
                 store_id=actor.store_id, action=action, target_type=object_type,
                 target_id=object_id, idempotency_key=f"stocktake:{action}:{object_id}",
                 details=details)


def create_session(session: Session, actor: AuthenticatedSubject, data: SessionCreate, *, key: str):
    _authorize(session, actor, "stocktake.write")
    payload = data.model_dump(mode="json")
    def execute():
        location_id = data.coverage_location_id
        if data.coverage_kind == "unknown" and location_id is not None:
            raise HTTPException(422, "stocktake_coverage_invalid")
        if data.coverage_kind != "unknown":
            if not location_id:
                raise HTTPException(422, "stocktake_coverage_invalid")
            location = session.scalar(select(Location).where(
                *_scope(Location, actor), Location.id == location_id, Location.active.is_(True)))
            if location is None:
                raise HTTPException(404, "stocktake_location_not_found")
        row = StocktakeSession(
            id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
            opened_by=actor.subject_id, coverage_kind=data.coverage_kind,
            coverage_location_id=location_id, coverage_note=data.coverage_note,
            status="open", version=1, created_at=_now())
        session.add(row)
        session.flush()
        _audit(session, actor, "stocktake.session.opened", "stocktake_session",
               row.id, {"version": 1, "coverage_kind": row.coverage_kind,
                        "coverage_location_id": row.coverage_location_id})
        return StocktakeSessionOut.model_validate(row).model_dump(mode="json")
    return _cmd(session, actor, "session.create", key, payload, execute)


def list_sessions(session: Session, actor: AuthenticatedSubject, limit: int = 50):
    _authorize(session, actor, "stocktake.read")
    rows = session.scalars(select(StocktakeSession).where(*_scope(StocktakeSession, actor))
                           .order_by(StocktakeSession.created_at.desc(), StocktakeSession.id)
                           .limit(limit)).all()
    return [StocktakeSessionOut.model_validate(row).model_dump(mode="json") for row in rows]


def get_session(session: Session, actor: AuthenticatedSubject, sid: str):
    _authorize(session, actor, "stocktake.read")
    return StocktakeSessionOut.model_validate(_session(session, actor, sid)).model_dump(mode="json")


def observe(session: Session, actor: AuthenticatedSubject, sid: str,
            data: ObservationCreate, *, key: str):
    _authorize(session, actor, "stocktake.write")
    payload = {"session_id": sid, **data.model_dump(mode="json")}
    def execute():
        row = _session(session, actor, sid)
        if row.status != "open" or row.version != data.expected_session_version:
            raise HTTPException(409, "stocktake_session_version_conflict")
        current = _now()
        at = data.observed_at.astimezone(timezone.utc)
        if at < current - timedelta(days=30) or at > current + timedelta(minutes=5):
            raise HTTPException(422, "stocktake_observation_time_invalid")
        if data.product_id is not None:
            product = session.scalar(select(CatalogProduct).where(
                *_scope(CatalogProduct, actor), CatalogProduct.id == data.product_id,
                CatalogProduct.active.is_(True)))
            if product is None:
                raise HTTPException(404, "stocktake_product_not_found")
        elif not data.unknown_product_label or not data.unknown_product_label.strip():
            raise HTTPException(422, "stocktake_unknown_product_invalid")
        if data.location_id is not None:
            location = session.scalar(select(Location).where(
                *_scope(Location, actor), Location.id == data.location_id,
                Location.active.is_(True)))
            if location is None:
                raise HTTPException(404, "stocktake_location_not_found")
        elif not data.unknown_location_label or not data.unknown_location_label.strip():
            raise HTTPException(422, "stocktake_unknown_location_invalid")
        if data.media_id is not None:
            image = session.scalar(select(MediaAsset).where(
                *_scope(MediaAsset, actor), MediaAsset.id == data.media_id,
                MediaAsset.category == "inventory_photo"))
            if image is None:
                raise HTTPException(404, "stocktake_media_not_found")
        if data.kind == "correction":
            prior = _event(session, actor, sid, data.supersedes_id)
            if prior.event_seq < 1:
                raise HTTPException(409, "stocktake_correction_invalid")
            already = session.scalar(select(StocktakeObservation.id).where(
                *_scope(StocktakeObservation, actor), StocktakeObservation.session_id == sid,
                StocktakeObservation.supersedes_id == prior.id))
            if already is not None:
                raise HTTPException(409, "stocktake_observation_superseded")
        count = session.execute(update(StocktakeSession).where(
            *_scope(StocktakeSession, actor), StocktakeSession.id == sid,
            StocktakeSession.status == "open",
            StocktakeSession.version == data.expected_session_version)
            .values(version=data.expected_session_version+1)
            .execution_options(synchronize_session=False)).rowcount
        if count != 1:
            raise HTTPException(409, "stocktake_session_version_conflict")
        # Bulk CAS bypasses ORM identity synchronization: never return a stale version.
        session.expire(row)
        session.refresh(row)
        item = StocktakeObservation(
            id=str(uuid4()), organization_id=actor.organization_id,
            store_id=actor.store_id, session_id=sid,
            kind=data.kind, supersedes_id=data.supersedes_id,
            product_id=data.product_id, unknown_product_label=data.unknown_product_label,
            location_id=data.location_id, unknown_location_label=data.unknown_location_label,
            quantity=str(data.quantity), unit=data.unit, media_id=data.media_id,
            observed_by=actor.subject_id, observed_at=at, recorded_at=current)
        session.add(item)
        session.flush()
        _audit(session, actor, "stocktake.observation.appended", "stocktake_observation",
               item.id, {"session_id": sid, "kind": item.kind,
                         "session_version": data.expected_session_version + 1})
        return ObservationOut.model_validate(item).model_dump(mode="json")
    return _cmd(session, actor, "observation.append", key, payload, execute)


def list_observations(session: Session, actor: AuthenticatedSubject, sid: str,
                      *, after_seq: int = 0, limit: int = 100):
    _authorize(session, actor, "stocktake.read")
    _session(session, actor, sid)
    rows = session.scalars(select(StocktakeObservation).where(
        *_scope(StocktakeObservation, actor), StocktakeObservation.session_id == sid,
        StocktakeObservation.event_seq > after_seq)
        .order_by(StocktakeObservation.event_seq.asc()).limit(limit)).all()
    return [ObservationOut.model_validate(row).model_dump(mode="json") for row in rows]
