"""Controlled sales draft API; no POS/Android/printing or upstream writes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.app.application.contracts import CommandIntent
from backend.app.application.dispatch import execute_http
from backend.app.core.idempotency import require_idempotency_key
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.sales import service
from backend.app.modules.sales.schemas import (
    DraftCreate, DraftEnvelope, DraftListEnvelope, HandoffCreate, HandoffEnvelope,
    LineCreate, ReceiptCreate, LineQuoteRequest, LineQuoteEnvelope,
)

router = APIRouter(prefix="/sales", tags=["sales"])


def _envelope(data, message: str, *, code: str = "OK") -> dict:
    return {"success": True, "data": data, "code": code, "error": None,
            "user_message": message, "hint": None}


def _run(action, message: str):
    try:
        data = action()
    except service.SalesError as error:
        raise HTTPException(status_code=(403 if error.code == "sales_permission_denied" else
                                        409 if error.code == "sales_version_conflict" else 400),
                            detail=error.code) from error
    return _envelope(data, message)


@router.get("/drafts", response_model=DraftListEnvelope)
def list_drafts_route(
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
) -> DraftListEnvelope:
    return _run(lambda: service.list_drafts(session, actor), "草稿列表")


@router.post("/drafts", response_model=DraftEnvelope, status_code=201)
def create_draft_route(
    payload: DraftCreate,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    key: Annotated[str, Depends(require_idempotency_key)],
) -> DraftEnvelope:
    with session.begin():
        result = _run(lambda: execute_http(session, actor, CommandIntent(action="sales.create_draft", payload=payload.model_dump(), idempotency_key=key)).data, "销售草稿已创建。")
    return DraftEnvelope.model_validate(result)


@router.get("/drafts/{draft_id}", response_model=DraftEnvelope)
def get_draft_route(
    draft_id: str,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
) -> DraftEnvelope:
    return _run(lambda: service.get_draft(session, actor, draft_id), "草稿详情")


@router.post("/drafts/{draft_id}/lines", response_model=DraftEnvelope)
def add_line_route(
    draft_id: str, payload: LineCreate,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
) -> DraftEnvelope:
    with session.begin():
        result = _run(lambda: service.add_line(session, actor, draft_id, payload), "草稿行已加入。")
    return DraftEnvelope.model_validate(result)


@router.post("/drafts/{draft_id}/lines/{line_id}/confirm", response_model=DraftEnvelope)
def confirm_line_route(
    draft_id: str, line_id: str,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
) -> DraftEnvelope:
    with session.begin():
        result = _run(lambda: service.confirm_line(session, actor, draft_id, line_id), "草稿行已确认。")
    return DraftEnvelope.model_validate(result)


@router.post("/drafts/{draft_id}/ready", response_model=DraftEnvelope)
def ready_route(
    draft_id: str,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
) -> DraftEnvelope:
    with session.begin():
        result = _run(lambda: service.mark_ready(session, actor, draft_id), "草稿已就绪。")
    return DraftEnvelope.model_validate(result)


@router.post("/drafts/{draft_id}/void", response_model=DraftEnvelope)
def void_route(
    draft_id: str,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
) -> DraftEnvelope:
    with session.begin():
        result = _run(lambda: service.void_draft(session, actor, draft_id), "草稿已撤销。")
    return DraftEnvelope.model_validate(result)


@router.post("/drafts/{draft_id}/handoffs", response_model=HandoffEnvelope, status_code=201)
def handoff_route(
    draft_id: str, payload: HandoffCreate,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    key: Annotated[str, Depends(require_idempotency_key)],
) -> HandoffEnvelope:
    with session.begin():
        data = _run(lambda: execute_http(session, actor, CommandIntent(action="sales.create_handoff", payload=payload.model_dump(), idempotency_key=key, target_id=draft_id)).data, "交接意图已创建。")["data"]
    return HandoffEnvelope(success=True, data=data, code="OK", error=None,
                           user_message="交接意图已创建。", hint=None)


@router.post("/handoffs/{handoff_id}/receipt", response_model=HandoffEnvelope)
def receipt_route(
    handoff_id: str, payload: ReceiptCreate,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    key: Annotated[str, Depends(require_idempotency_key)],
) -> HandoffEnvelope:
    with session.begin():
        data = _run(lambda: execute_http(session, actor, CommandIntent(action="sales.record_receipt", payload=payload.model_dump(), idempotency_key=key, target_id=handoff_id)).data, "交接回执已记录。")["data"]
    return HandoffEnvelope(success=True, data=data, code="OK", error=None,
                           user_message="交接回执已记录。", hint=None)


@router.post("/drafts/{draft_id}/lines/{line_key}/quote", response_model=LineQuoteEnvelope)
def quote_line_route(
    draft_id: str, line_key: str, payload: LineQuoteRequest,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
) -> LineQuoteEnvelope:
    with session.begin():
        result = _run(lambda: service.quote_line(session, actor, draft_id, line_key, payload), "报价已生成。")
    return LineQuoteEnvelope.model_validate(result)


from fastapi import Query
from fastapi.responses import JSONResponse
from backend.app.core.errors import ApiEnvelope
from backend.app.modules.sales.schemas import (
    LineQuoteRequestV2, LineBindRequestV2, ContextEnvelopeV2, QuoteEnvelopeV2, LineEnvelopeV2,
)


def _run_v2(action):
    try:
        return _envelope(action(), "原键事实与当前可用性；未知结果不能视为未发送。")
    except service.SalesError as error:
        status = (403 if error.code == "sales_permission_denied" else
                  404 if error.code == "sales_draft_not_found" else
                  409 if error.code in {"sales_v2_key_conflict", "sales_version_conflict", "sales_v2_line_exists"} else 400)
        body = ApiEnvelope(success=False, data=None, code=error.code.upper(), error=error.code,
            user_message="当前身份或报价条件不满足要求，操作未完成。",
            hint="仅按原键查询已保存事实；未知结果不要换键重派。")
        return JSONResponse(status_code=status, content=body.model_dump(), headers={"Cache-Control": "no-store"})


def _v2_transaction(session, action):
    # Root-commit guards can reject after the service returns. Preserve the
    # exact v2 rejection envelope only after rollback, never a premature result.
    with session.begin():
        return action()


_V2_ERRORS = {status: {"model": ApiEnvelope} for status in (400, 403, 404, 409)}


@router.get("/quote-context-v2", response_model=ContextEnvelopeV2, responses=_V2_ERRORS)
def quote_context_v2_route(
    catalog_product_id: Annotated[str, Query(min_length=1, max_length=36)],
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
):
    return _run_v2(lambda: service.quote_context_v2(session, actor, catalog_product_id))


@router.post("/drafts/{draft_id}/line-quotes-v2", response_model=QuoteEnvelopeV2, responses=_V2_ERRORS)
def quote_line_v2_route(
    draft_id: str, payload: LineQuoteRequestV2,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    key: Annotated[str, Depends(require_idempotency_key)],
):
    return _run_v2(lambda: _v2_transaction(session, lambda: execute_http(session, actor, CommandIntent(
        action="sales.quote_line_v2", payload=payload.model_dump(), target_id=draft_id, idempotency_key=key)).data))


@router.get("/drafts/{draft_id}/line-quotes-v2", response_model=QuoteEnvelopeV2, responses=_V2_ERRORS)
def get_quote_v2_route(
    draft_id: str,
    quote_key: Annotated[str, Query(min_length=1, max_length=200)],
    line_key: Annotated[str, Query(min_length=1, max_length=200)],
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
):
    return _run_v2(lambda: service.get_line_quote_v2(session, actor, draft_id, quote_key, line_key))


@router.post("/drafts/{draft_id}/lines-v2", response_model=LineEnvelopeV2, responses=_V2_ERRORS)
def bind_line_v2_route(
    draft_id: str, payload: LineBindRequestV2,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    key: Annotated[str, Depends(require_idempotency_key)],
):
    return _run_v2(lambda: _v2_transaction(session, lambda: execute_http(session, actor, CommandIntent(
        action="sales.bind_line_v2", payload=payload.model_dump(), target_id=draft_id, idempotency_key=key)).data))


@router.get("/drafts/{draft_id}/line-commands-v2", response_model=LineEnvelopeV2, responses=_V2_ERRORS)
def get_line_command_v2_route(
    draft_id: str,
    line_key: Annotated[str, Query(min_length=1, max_length=200)],
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
):
    return _run_v2(lambda: service.get_line_command_v2(session, actor, draft_id, line_key))
