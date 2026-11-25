"""ANA-002 read-only scoped metric snapshots. No materialization on GET."""
from typing import Annotated
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.app.core.errors import ApiEnvelope
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.analytics import service

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/snapshots", response_model=ApiEnvelope)
def list_route(
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> ApiEnvelope:
    items = service.list_snapshots(session, actor, limit=limit)
    return ApiEnvelope(
        success=True, data={"items": items, "count": len(items)},
        code="OK", error=None,
        user_message="局部盘点指标为候选来源快照，不代表全店账面库存。",
        hint=None,
    )


@router.get("/snapshots/{snapshot_id}", response_model=ApiEnvelope)
def read_route(
    snapshot_id: str,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
) -> ApiEnvelope:
    item = service.read_snapshot(session, actor, snapshot_id)
    return ApiEnvelope(
        success=True, data=item, code="OK", error=None,
        user_message="可查看来源与时效；未知/部分覆盖不得作为库存准确率。",
        hint=None,
    )
