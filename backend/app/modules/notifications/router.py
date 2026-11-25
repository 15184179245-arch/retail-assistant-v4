"""Employee-scoped inbox, CAS read receipt and explicit overdue reconciliation."""
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.app.core.idempotency import require_idempotency_key
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.notifications import service
from backend.app.modules.notifications.schemas import (
    MarkRead, NotificationEnvelope, NotificationListEnvelope, NotificationOut,
    ReconcileEnvelope, ReconcileOut,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _wrap(data, model):
    return model(success=True, data=data, code="OK", error=None,
                 user_message="仅显示本店当前本人有权查看的站内提醒。", hint=None)


@router.get("", response_model=NotificationListEnvelope)
def inbox(actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
          session: Annotated[Session, Depends(get_session)],
          limit: Annotated[int, Query(ge=1, le=100)] = 50) -> NotificationListEnvelope:
    return _wrap([NotificationOut.model_validate(row) for row in
                  service.list_inbox(session, actor, limit=limit)], NotificationListEnvelope)


@router.post("/reconcile-overdue", response_model=ReconcileEnvelope)
def overdue(actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
            session: Annotated[Session, Depends(get_session)],
            key: Annotated[str, Depends(require_idempotency_key)],
            limit: Annotated[int, Query(ge=1, le=100)] = 100) -> ReconcileEnvelope:
    # A request key is mandatory even though the underlying event dedupe is stable.
    # This is a privileged explicit operation, not a background sender.
    try:
        result = service.reconcile_overdue(session, actor, key=key, limit=limit)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return _wrap(ReconcileOut(**result), ReconcileEnvelope)


@router.post("/{notification_id}/read", response_model=NotificationEnvelope)
def read(notification_id: str, payload: MarkRead,
         actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
         session: Annotated[Session, Depends(get_session)],
         key: Annotated[str, Depends(require_idempotency_key)]) -> NotificationEnvelope:
    try:
        row = service.mark_read(session, actor, notification_id,
                                expected_version=payload.expected_version, idempotency_key=key)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return _wrap(NotificationOut.model_validate(row), NotificationEnvelope)
