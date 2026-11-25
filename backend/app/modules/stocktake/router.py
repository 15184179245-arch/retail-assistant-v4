"""Employee-only stocktake observations, never an inventory adjustment."""
from typing import Annotated
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from backend.app.core.idempotency import require_idempotency_key
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.stocktake import service, review_service
from backend.app.modules.stocktake.schemas import (
    SessionCreate, ObservationCreate, SessionEnvelope, SessionsEnvelope,
    ObservationEnvelope, ObservationsEnvelope, StocktakeSessionOut, ObservationOut,
    ConversionVerify, ReviewDecision, BlindSessionView, ReviewPreviewView,
    ConversionReceiptView, ReviewReceiptView, BlindSessionEnvelope,
    ReviewPreviewEnvelope, ConversionReceiptEnvelope, ReviewReceiptEnvelope,
)

router = APIRouter(prefix="/stocktake", tags=["stocktake"])


def _wrap(data, cls):
    return cls(success=True, data=data, code="OK", error=None,
               user_message="局部盘点记录已保存，不代表全店库存或账面调整。", hint=None)


@router.post("/sessions", response_model=SessionEnvelope, status_code=201)
def create_route(payload: SessionCreate,
                 actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                 session: Annotated[Session, Depends(get_session)],
                 key: Annotated[str, Depends(require_idempotency_key)]) -> SessionEnvelope:
    try:
        row = service.create_session(session, actor, payload, key=key)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return _wrap(StocktakeSessionOut.model_validate(row), SessionEnvelope)


@router.get("/sessions", response_model=SessionsEnvelope)
def list_route(actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
               session: Annotated[Session, Depends(get_session)],
               limit: Annotated[int, Query(ge=1, le=100)] = 50) -> SessionsEnvelope:
    return _wrap([StocktakeSessionOut.model_validate(v) for v in
                  service.list_sessions(session, actor, limit)], SessionsEnvelope)


@router.get("/sessions/{session_id}", response_model=SessionEnvelope)
def get_route(session_id: str,
              actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
              session: Annotated[Session, Depends(get_session)]) -> SessionEnvelope:
    return _wrap(StocktakeSessionOut.model_validate(
        service.get_session(session, actor, session_id)), SessionEnvelope)


@router.post("/sessions/{session_id}/observations", response_model=ObservationEnvelope, status_code=201)
def append_route(session_id: str, payload: ObservationCreate,
                 actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                 session: Annotated[Session, Depends(get_session)],
                 key: Annotated[str, Depends(require_idempotency_key)]) -> ObservationEnvelope:
    try:
        row = service.observe(session, actor, session_id, payload, key=key)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return _wrap(ObservationOut.model_validate(row), ObservationEnvelope)


@router.get("/sessions/{session_id}/observations", response_model=ObservationsEnvelope)
def observations_route(session_id: str,
                       actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                       session: Annotated[Session, Depends(get_session)],
                       after_seq: Annotated[int, Query(ge=0)] = 0,
                       limit: Annotated[int, Query(ge=1, le=100)] = 100) -> ObservationsEnvelope:
    return _wrap([ObservationOut.model_validate(v) for v in
                  service.list_observations(session, actor, session_id,
                                            after_seq=after_seq, limit=limit)], ObservationsEnvelope)


# STK-004 read-only blind view NEVER returns an ERP/book inventory count.
@router.get("/sessions/{session_id}/blind", response_model=BlindSessionEnvelope)
def blind_route(session_id: str,
                actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                session: Annotated[Session, Depends(get_session)]) -> BlindSessionEnvelope:
    return _wrap(BlindSessionView.model_validate(
        review_service.blind_session(session, actor, session_id)), BlindSessionEnvelope)


@router.get("/sessions/{session_id}/review-preview", response_model=ReviewPreviewEnvelope)
def review_preview_route(session_id: str,
                         actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                         session: Annotated[Session, Depends(get_session)]) -> ReviewPreviewEnvelope:
    return _wrap(ReviewPreviewView.model_validate(
        review_service.review_preview(session, actor, session_id)), ReviewPreviewEnvelope)


@router.post("/sessions/{session_id}/conversions", response_model=ConversionReceiptEnvelope,
             status_code=201)
def conversion_route(session_id: str, payload: ConversionVerify,
                     actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                     session: Annotated[Session, Depends(get_session)],
                     key: Annotated[str, Depends(require_idempotency_key)]) -> ConversionReceiptEnvelope:
    try:
        result = review_service.verify_conversion(session, actor, session_id, payload, key=key)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return _wrap(ConversionReceiptView.model_validate(result), ConversionReceiptEnvelope)


@router.post("/sessions/{session_id}/reviews", response_model=ReviewReceiptEnvelope,
             status_code=201)
def review_route(session_id: str, payload: ReviewDecision,
                 actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                 session: Annotated[Session, Depends(get_session)],
                 key: Annotated[str, Depends(require_idempotency_key)]) -> ReviewReceiptEnvelope:
    try:
        result = review_service.review_decision(session, actor, session_id, payload, key=key)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return _wrap(ReviewReceiptView.model_validate(result), ReviewReceiptEnvelope)
