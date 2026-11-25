from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.app.modules.identity.service import authorize_scope
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.search import service
from backend.app.modules.search.schemas import SearchEnvelope, SearchResultOut

router = APIRouter(prefix="/search", tags=["search"])


@router.get("", response_model=SearchEnvelope)
def search_route(
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
) -> SearchEnvelope:
    authorize_scope(session, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission="catalog.read")
    result = service.search_products(
        session, organization_id=actor.organization_id,
        store_id=actor.store_id, query=q, limit=limit)
    return SearchEnvelope(
        success=True, data=SearchResultOut(**result), code="OK", error=None,
        user_message=result["message"], hint=None,
    )
