"""AGT-013: deterministic catalog search exposed as a read-only Agent tool."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.search import service as search_service
from backend.app.modules.search.refinement import SearchPlan
from backend.app.modules.search.hybrid import search_hybrid
from backend.app.modules.search.schemas import SearchDisplayText


class CatalogSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=10, ge=1, le=20)
    plan: SearchPlan | None = None


class CatalogMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str
    external_code: str
    match_field: Literal["code", "barcode", "alias", "name", "derived"]
    match_type: str
    original_value: str
    normalized_value: str | None
    name: SearchDisplayText
    spec: SearchDisplayText
    unit: SearchDisplayText
    barcode: str | None
    source_system: str
    source_version: str
    source_updated_at: datetime | None
    captured_at: datetime
    search_evidence: list[dict] = Field(default_factory=list)


class CatalogSearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    exact: list[CatalogMatch]
    candidates: list[CatalogMatch]
    ambiguous: bool
    message: str
    exact_total: int
    candidate_total: int
    has_more: bool
    scan_truncated: bool
    truncated: bool = False


def search(session: Session, requester: AuthenticatedSubject,
           payload: CatalogSearchInput) -> CatalogSearchOutput:
    if payload.plan is not None:
        return CatalogSearchOutput(**search_hybrid(session,
            organization_id=requester.organization_id,store_id=requester.store_id,
            query=payload.query,plan=payload.plan,limit=payload.limit))
    result = search_service.search_products(
        session, organization_id=requester.organization_id,
        store_id=requester.store_id, query=payload.query, limit=payload.limit)
    return CatalogSearchOutput(**result)
