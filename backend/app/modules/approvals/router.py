from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.exceptions import HTTPException
from sqlalchemy.orm import Session

from backend.app.core.idempotency import require_idempotency_key
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.approvals.schemas import (
    ApprovalCancellation,
    ApprovalCreate,
    ApprovalDecision,
    ApprovalEnvelope,
    ApprovalListEnvelope,
    ApprovalResponse,
)
from backend.app.modules.approvals.service import (
    cancel_approval,
    decide_approval,
    expire_approval,
    get_approval_for_read,
    list_approvals,
    request_approval,
)

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _envelope(approval, message: str = "审批请求状态已更新。") -> ApprovalEnvelope:
    return ApprovalEnvelope(
        success=True,
        data=ApprovalResponse.model_validate(approval),
        code="OK",
        error=None,
        user_message=message,
        hint=None,
    )


@router.get("", response_model=ApprovalListEnvelope)
def list_route(
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    state: Literal["requested", "approved", "rejected", "cancelled", "expired"] | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: Annotated[str | None, Query(min_length=1, max_length=36)] = None,
) -> ApprovalListEnvelope:
    items, next_cursor = list_approvals(
        session, actor, state=state, limit=limit, cursor=cursor,
    )
    return ApprovalListEnvelope(
        success=True,
        data={"items": [ApprovalResponse.model_validate(item) for item in items],
              "next_cursor": next_cursor},
        code="OK", error=None, user_message="审批请求已读取。", hint=None,
    )


@router.get("/{approval_id}", response_model=ApprovalEnvelope)
def read_route(
    approval_id: str,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
) -> ApprovalEnvelope:
    approval = get_approval_for_read(session, actor, approval_id=approval_id)
    return _envelope(approval, "审批请求已读取。")


@router.post("", response_model=ApprovalEnvelope, status_code=201)
def create_approval(
    payload: ApprovalCreate,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
) -> ApprovalEnvelope:
    with session.begin():
        approval = request_approval(
            session, actor,
            subject_type=payload.subject_type,
            subject_id=payload.subject_id,
            action_key=payload.action_key,
            reason=payload.reason,
            expires_at=payload.expires_at,
            idempotency_key=idempotency_key,
        )
    return _envelope(approval, "审批请求已创建。")


@router.post("/{approval_id}/decision", response_model=ApprovalEnvelope)
def decide(
    approval_id: str,
    payload: ApprovalDecision,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
) -> ApprovalEnvelope:
    with session.begin():
        approval = decide_approval(
            session, actor, approval_id=approval_id,
            decision=payload.decision, reason=payload.reason,
            idempotency_key=idempotency_key,
        )
    if approval.state == "expired":
        raise HTTPException(status_code=409, detail="approval_expired")
    return _envelope(approval)


@router.post("/{approval_id}/cancel", response_model=ApprovalEnvelope)
def cancel(
    approval_id: str,
    payload: ApprovalCancellation,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
) -> ApprovalEnvelope:
    with session.begin():
        approval = cancel_approval(
            session, actor, approval_id=approval_id,
            reason=payload.reason, idempotency_key=idempotency_key,
        )
    if approval.state == "expired":
        raise HTTPException(status_code=409, detail="approval_expired")
    return _envelope(approval, "审批请求已取消。")


@router.post("/{approval_id}/expire", response_model=ApprovalEnvelope)
def expire(
    approval_id: str,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
) -> ApprovalEnvelope:
    with session.begin():
        approval = expire_approval(session, actor, approval_id=approval_id, idempotency_key=idempotency_key)
    return _envelope(approval, "审批请求已过期。")
