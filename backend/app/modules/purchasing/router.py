"""Employee-only manual purchasing intents; no formal PO or receipt writes."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.app.core.idempotency import require_idempotency_key
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.purchasing import service
from backend.app.modules.purchasing.schemas import (
    PurchaseIntentCreate, PurchaseIntentEnvelope, PurchaseIntentListEnvelope,
    PurchaseLineConfirm,
)

router = APIRouter(prefix="/purchasing", tags=["purchasing"])


def _run(fn, message: str):
    try:
        result = fn()
    except service.PurchaseError as error:
        status = (404 if error.code in ("purchase_not_found", "purchase_line_not_found") else
                  409 if error.code in ("purchase_version_conflict", "purchase_duplicate_evidence",
                                       "purchase_line_already_confirmed",
                                       "purchase_product_version_conflict") else
                  403 if error.code in ("purchase_human_required", "purchase_consent_required") else 400)
        raise HTTPException(status_code=status, detail=error.code) from error
    return {"success": True, "data": result, "code": "OK", "error": None,
            "user_message": message, "hint": None}


@router.post("/intents", response_model=PurchaseIntentEnvelope, status_code=201)
def create_intent_route(
    payload: PurchaseIntentCreate,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    key: Annotated[str, Depends(require_idempotency_key)],
) -> PurchaseIntentEnvelope:
    with session.begin():
        data = _run(lambda: service.create_intent(session, actor, payload,
                                                   idempotency_key=key), "待到货意向已记录。")
    return PurchaseIntentEnvelope.model_validate(data)


@router.get("/intents", response_model=PurchaseIntentListEnvelope)
def list_intents_route(
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
) -> PurchaseIntentListEnvelope:
    return PurchaseIntentListEnvelope.model_validate(
        _run(lambda: service.list_intents(session, actor), "待到货意向列表。"))


@router.get("/intents/{intent_id}", response_model=PurchaseIntentEnvelope)
def get_intent_route(
    intent_id: str,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
) -> PurchaseIntentEnvelope:
    return PurchaseIntentEnvelope.model_validate(
        _run(lambda: service.get_intent(session, actor, intent_id), "待到货意向详情。"))


@router.post("/intents/{intent_id}/lines/{line_id}/confirm", response_model=PurchaseIntentEnvelope)
def confirm_line_route(
    intent_id: str,
    line_id: str,
    payload: PurchaseLineConfirm,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    key: Annotated[str, Depends(require_idempotency_key)],
) -> PurchaseIntentEnvelope:
    with session.begin():
        data = _run(lambda: service.confirm_line(session, actor, intent_id, line_id,
                                                  payload, idempotency_key=key),
                    "商品和数量已由员工确认。")
    return PurchaseIntentEnvelope.model_validate(data)
