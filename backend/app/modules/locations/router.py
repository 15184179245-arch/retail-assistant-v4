from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.locations import service
from backend.app.modules.locations.schemas import (AssertionCreate, AssertionEnvelope,
                                                   AssertionListEnvelope, AssertionOut,
                                                   LocationCreate, LocationEnvelope,
                                                   LocationOut)

router = APIRouter(prefix="/locations", tags=["locations"])


def _http(error: service.LocationError) -> HTTPException:
    status = 404 if error.code in ("location_not_found", "location_product_not_found") else (
        403 if error.code == "location_permission_denied" else 400)
    return HTTPException(status_code=status, detail=error.code)


@router.post("", response_model=LocationEnvelope, status_code=201)
def create_route(
    payload: LocationCreate,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
) -> LocationEnvelope:
    try:
        location = service.create_location(session, actor, name=payload.name,
                                           kind=payload.kind, parent_id=payload.parent_id)
        session.commit()
    except service.LocationError as error:
        session.rollback()
        raise _http(error) from error
    return LocationEnvelope(success=True, data=LocationOut.model_validate(location),
                            code="OK", error=None, user_message="库位已创建。", hint=None)


@router.post("/{location_id}/assertions", response_model=AssertionEnvelope, status_code=201)
def assert_route(
    location_id: str,
    payload: AssertionCreate,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
) -> AssertionEnvelope:
    try:
        assertion = service.assert_placement(
            session, actor, location_id=location_id, product_id=payload.product_id,
            kind=payload.kind, idempotency_key=payload.idempotency_key)
        session.commit()
    except service.LocationError as error:
        session.rollback()
        raise _http(error) from error
    return AssertionEnvelope(success=True, data=AssertionOut.model_validate(assertion),
                             code="OK", error=None, user_message="放置断言已记录。", hint=None)


@router.get("/{location_id}/assertions", response_model=AssertionListEnvelope)
def history_route(
    location_id: str,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    limit: int = Query(default=100, ge=1, le=500),
) -> AssertionListEnvelope:
    try:
        rows = service.placement_history(session, actor, location_id=location_id, limit=limit)
    except service.LocationError as error:
        raise _http(error) from error
    return AssertionListEnvelope(
        success=True, data={"items": [AssertionOut.model_validate(row).model_dump(mode="json") for row in rows]},
        code="OK", error=None, user_message="放置历史已读取。", hint=None)
