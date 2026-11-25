from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.identity.schemas import IdentityContext, IdentityContextEnvelope
from backend.app.modules.identity.service import authorize_scope, resolve_effective_capabilities

router = APIRouter(prefix="/identity", tags=["identity"])


@router.get("/context", response_model=IdentityContextEnvelope)
def identity_context(
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    response: Response,
) -> IdentityContextEnvelope:
    authorize_scope(session, actor, organization_id=actor.organization_id, store_id=actor.store_id)
    capabilities, cap_version = resolve_effective_capabilities(session, actor)
    envelope = IdentityContextEnvelope(
        success=True,
        data=IdentityContext(
            subject_id=actor.subject_id,
            organization_id=actor.organization_id,
            store_id=actor.store_id,
            auth_expires_at=actor.auth_expires_at,
            capabilities=capabilities,
            capability_version=cap_version,
        ),
        code="OK",
        error=None,
        user_message="身份范围已确认。",
        hint=None,
    )
    response.headers["Cache-Control"] = "no-store"
    return envelope
