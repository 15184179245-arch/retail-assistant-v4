from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.audit.schemas import AuditEventItem, AuditEventPage, AuditEventsEnvelope
from backend.app.modules.audit.service import query_events

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/events", response_model=AuditEventsEnvelope)
def read_events(
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=36)] = None,
) -> AuditEventsEnvelope:
    events, next_cursor = query_events(session, actor, limit=limit, cursor=cursor)
    return AuditEventsEnvelope(
        success=True,
        data=AuditEventPage(
            items=[AuditEventItem.model_validate(event) for event in events],
            next_cursor=next_cursor,
        ),
        code="OK",
        error=None,
        user_message="审计记录已读取。",
        hint=None,
    )
